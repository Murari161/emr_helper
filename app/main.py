"""Chainlit entry point for the eAFYA Assistant.

The Phase 5 wiring:
    on_chat_start
        - read the logged-in account
        - create a conversations row for this session
        - greet + show starters
    on_message
        - show a "Searching the manuals…" step
        - retrieve top-5 chunks
        - persist the user turn
        - if the best match is weak, reply with a fixed "off-topic" / "contact
          your eAFYA Focal Person" message instead of generating (fallback.py)
        - otherwise stream the LLM answer into a Chainlit message
        - after streaming, put each screenshot under the step it
          illustrates (from the chunks the answer actually drew on), append
          a citation footer, persist the assistant turn

Logins (stakeholder item 5): Chainlit's login page checks the username and
password against app_users (app/auth.py). Every question re-reads the account,
so a disabled account or a role change takes effect at once. Ordinary users
are not answered from the restricted manuals (app/rag/access.py); super users
see everything and manage accounts at /admin (app/ui/admin.py).
"""
from __future__ import annotations

import logging
import re
from typing import Any
from uuid import UUID

import chainlit as cl

from app import auth
from app.config import settings
from app.db import queries
from app.rag import access, attribution, callouts, classifier, fallback, reranker, topics
from app.rag.generator import stream_answer
from app.rag.retriever import RetrievalResult, retrieve_scored
from app.ui import admin
from app.ui.figures import answer_with_figures
from app.ui.starters import get_starters
from app.ui.welcome import welcome_message

# Eagerly load the cross-encoder at import time so the first user query
# doesn't pay a multi-second model-load + first-time HuggingFace download.
# With the hf_cache docker volume mounted, this only does network work on
# the very first start ever; subsequent starts are instant.
reranker.warmup()

logging.basicConfig(
    level=settings.app_log_level,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("emr_helper")


# ---------------------------------------------------------------------------
# Serve manual screenshots over HTTP.
# Images are extracted to settings.images_dir (/data/images/<slug>/fig_NN.png).
# cl.Image elements can read local paths directly, but to embed each screenshot
# INLINE in the answer — under its own caption, in document order — we need a
# URL. Mount the images directory as static files at settings.images_url_prefix.
# ---------------------------------------------------------------------------
try:
    from starlette.staticfiles import StaticFiles
    from starlette.routing import Mount
    from chainlit.server import app as _fastapi_app

    if settings.images_dir.is_dir():
        # Insert at the FRONT of the route table. Chainlit registers a catch-all
        # route that serves its single-page app for any unmatched path, and that
        # catch-all is already present by the time this module imports. A plain
        # app.mount() appends AFTER it, so /images/* gets shadowed by the SPA
        # (the browser receives HTML, not the PNG → broken image). Inserting the
        # static mount first gives it priority over the catch-all.
        # Behind the login: only logged-in users get the screenshots.
        _fastapi_app.router.routes.insert(
            0,
            Mount(
                settings.images_url_prefix,
                app=admin.LoginRequired(StaticFiles(directory=str(settings.images_dir))),
                name="manual-images",
            ),
        )
        log.info(
            "Serving manual images at %s from %s (priority route)",
            settings.images_url_prefix,
            settings.images_dir,
        )
    else:
        log.warning("Images dir %s not found; screenshots will not render", settings.images_dir)
except Exception:  # noqa: BLE001 — never let static wiring crash app startup
    log.exception("Could not mount manual images; screenshots may not render inline")

# Account pages: /admin (super users) and /account (everyone).
from chainlit.server import app as _server_app  # noqa: E402

admin.register(_server_app)


# ---------------------------------------------------------------------------
# Authentication (stakeholder item 5).
#
# One login per person, checked against app_users. Caddy no longer asks for a
# password (its pop-up login could not tell people apart); it only provides
# HTTPS. Repeated wrong passwords lock the username for a while (auth.LoginGuard).
#
# LOGIN_REQUIRED=false (facility installations): there is no login page. The
# chat lets everyone in as "guest", with the ordinary user's limits, through
# Chainlit's header login; a super user who has logged in at /account/login is
# recognised by their staff cookie (app/ui/admin.py). Logging out clears it.
# ---------------------------------------------------------------------------


async def password_login(username: str, password: str) -> cl.User | None:
    name = auth.normalise_username(username)
    if auth.login_guard.is_locked(name):
        log.warning("login refused: %r is locked after repeated wrong passwords", name)
        return None
    try:
        account = await queries.get_app_user(name)
    except Exception:
        log.exception("login: could not read the account")
        return None
    # A wrong username costs the same time as a wrong password.
    ok = auth.verify_password(password, account["password_hash"] if account else auth.DUMMY_HASH)
    if not (ok and account and account["active"]):
        auth.login_guard.failed(name)
        log.info("login failed for %r", name)
        return None
    auth.login_guard.succeeded(name)
    try:
        await queries.touch_app_user_login(name)
    except Exception:
        log.exception("login: could not record the login time")
    log.info("login: %r (%s)", name, account["role"])
    return cl.User(identifier=name, display_name=account["full_name"] or name, metadata={"role": account["role"]})


async def open_access(headers: Any) -> cl.User:
    """Facility installation: a super user with a staff cookie, else a guest."""
    try:
        account = await admin.staff_account(headers.get("cookie"))
    except Exception:
        log.exception("open access: could not read the staff login; continuing as a guest")
        account = None
    if account:
        return cl.User(identifier=account["username"], display_name=account["full_name"] or account["username"],
                       metadata={"role": account["role"]})
    return cl.User(identifier=auth.GUEST, display_name="Guest", metadata={"role": auth.USER})


async def forget_staff_login(request: Any, response: Any) -> dict[str, bool]:
    """Logging out at a facility installation also ends the super-user login."""
    admin.clear_staff_cookie(response)
    return {"success": True}


if settings.login_required:
    cl.password_auth_callback(password_login)
else:
    cl.header_auth_callback(open_access)
    cl.on_logout(forget_staff_login)
    log.info("LOGIN_REQUIRED=false: no login for normal users; super users log in at /account/login")


async def _current_account() -> dict[str, Any] | None:
    """The logged-in account, read fresh from the database (None if unknown)."""
    user = cl.user_session.get("user")
    if not isinstance(user, cl.User):
        return None
    if user.identifier == auth.GUEST and not settings.login_required:
        return dict(access.GUEST_ACCOUNT)
    try:
        return await queries.get_app_user(user.identifier)
    except Exception:
        log.exception("Could not read the account of %r", user.identifier)
        return None


async def _gate() -> tuple[dict[str, Any] | None, list[str]]:
    """Check the account before answering: (account, manuals it may not be
    answered from), or (None, []) after telling the user why it cannot ask."""
    account = await _current_account()
    blocked = access.account_block_reply(account)
    if blocked:
        await cl.Message(content=blocked).send()
        return None, []
    return account, access.excluded_manuals(account["role"], settings.restricted_manuals)


# ---------------------------------------------------------------------------
# Chat lifecycle
# ---------------------------------------------------------------------------

@cl.set_starters
async def set_starters():
    return get_starters()


@cl.on_chat_start
async def on_chat_start() -> None:
    account = await _current_account()
    user_id = account["username"] if account else "anonymous"

    conv_id = await queries.create_conversation(user_id=user_id)
    cl.user_session.set("conversation_id", conv_id)

    log.info("on_chat_start: user=%s, conversation=%s", user_id, conv_id)

    await cl.Message(content=welcome_message(trial_notice=settings.trial_notice)).send()
    if account and account["must_change_password"]:
        await cl.Message(content=access.CHANGE_PASSWORD_REPLY).send()


# ---------------------------------------------------------------------------
# Per-message handler
# ---------------------------------------------------------------------------

# The chunk-attribution heuristic: which retrieved chunks did the LLM actually
# answer from? We approximate by checking whether the chunk's title (or any
# substantial bold UI label) appears as a substring of the answer text.
_LABEL_TOKEN_RE = re.compile(r"\S+")


# Greetings and thanks should not trigger retrieval (fallback.is_chitchat).
# Without this guard, typing "okay" sends "okay" through vector + BM25 + trigram
# and produces a confident-looking answer drawn from whichever chunk happened
# to top-rank. That's a confusing UX failure.
_FILLER_REPLY = (
    "Hello! 👋 I'm the **eAFYA Assistant** — I answer questions about how to "
    "use eAFYA from the official user manuals.\n\n"
    "Try something specific, for example:\n"
    "- *How do I register a new patient?*\n"
    "- *How do I close a clinic session?*\n"
    "- *How do I schedule an appointment reminder?*"
)


def _chunks_referenced_by(answer: str, chunks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Pick the chunk(s) the answer drew on, for images + the citation.

    First choice: the one chunk whose text the answer reproduces
    (attribution.source_chunk). That is what keeps a twin procedure from
    another manual, or a procedure the answer only mentions, from supplying
    the screenshots and the citation.

    If no chunk clearly is the source (a heavily paraphrased answer), fall
    back on titles. Only 'procedure' chunks carry screenshots; 'index_entry' /
    'glossary' / 'section_intro' chunks are text-only. A "How do I X?" query
    often ranks the Quick-Index entry (same title, 0 images) above the real
    procedure, so the fallback PREFERS procedures:
      1. procedure chunks whose title appears in the answer,
      2. the highest-ranked procedure chunk retrieved (model may have paraphrased),
      3. any chunk whose title appears in the answer,
      4. the top-ranked chunk.
    """
    if not chunks:
        return []
    source = attribution.source_chunk(answer, chunks)
    if source is not None:
        return [source]

    answer_lower = answer.lower()
    titled = [c for c in chunks if c.get("title") and c["title"].lower() in answer_lower]

    proc_titled = [c for c in titled if c.get("kind") == "procedure"]
    if proc_titled:
        return proc_titled

    procs = [c for c in chunks if c.get("kind") == "procedure"]
    if procs:
        return [procs[0]]

    if titled:
        return titled
    return [chunks[0]]


def _citation(top_chunk: dict[str, Any] | None) -> str | None:
    if not top_chunk:
        return None
    sp = top_chunk.get("section_path", "")
    if not sp:
        return None
    return f"\n\n— *Source: {sp}*"


# --- Cross-reference buttons ----------------------------------------------
# Procedures carry "See:"/"See module:" markers pointing at the canonical
# workflow elsewhere. We surface each as a clickable button under the answer;
# clicking it re-runs retrieval for that target and posts the linked workflow.
_SEE_RE = re.compile(r"^\s*See(?:\s+module)?:\s*(.+?)\s*$", re.IGNORECASE | re.MULTILINE)


def _reference_actions(chunks_used: list[dict[str, Any]]) -> list[cl.Action]:
    """One 'Open: <target>' button per unique cross-reference in the used chunks."""
    targets: list[str] = []
    seen: set[str] = set()
    for c in chunks_used:
        content = c.get("content") or ""
        for m in _SEE_RE.finditer(content):
            tgt = m.group(1).strip().rstrip(".")
            key = tgt.lower()
            if tgt and key not in seen:
                seen.add(key)
                targets.append(tgt)
    actions: list[cl.Action] = []
    for tgt in targets[:6]:  # cap so we never render a wall of buttons
        actions.append(
            cl.Action(
                name="open_reference",
                payload={"query": tgt},
                label=f"📄 Open: {tgt}",
                tooltip=f"Show the '{tgt}' workflow from the manuals",
            )
        )
    return actions


def _topic_actions(options: list[dict[str, Any]]) -> list[cl.Action]:
    """One button per offered topic. The payload pins the exact procedure, since
    the same title can exist in several manuals."""
    actions: list[cl.Action] = []
    for c in options:
        manual = re.sub(r"\s+Module$", "", c.get("manual") or "")
        actions.append(
            cl.Action(
                name="open_topic",
                payload={"chunk_id": str(c["id"]), "title": c.get("title") or ""},
                label=f"📄 Open: {c.get('title')} ({manual})",
                tooltip=f"Show the steps for '{c.get('title')}'",
            )
        )
    return actions


async def _respond(
    query: str,
    conv_id: UUID | None,
    *,
    excluded: list[str],
    prefer_procedures: bool = False,
    pinned: dict[str, Any] | None = None,
) -> None:
    """Retrieve → stream an answer → append captioned screenshots, a citation,
    and clickable cross-reference buttons → persist. Shared by on_message and
    the cross-reference action callback so both behave identically.

    prefer_procedures: set True for cross-reference clicks. A "See module: X"
    click searches a broad module name, which can match the module's text-only
    Quick Index / overview (no screenshots). Preferring procedure-kind chunks
    makes the opened workflow land on a real procedure that carries its images.

    pinned: answer from exactly this chunk, without searching (a topic the user
    chose from a list).

    excluded: manuals this user may not be answered from (app/rag/access.py).
    """
    import time  # perf profiling (perf/latency-profiling branch)
    _t_req = time.perf_counter()

    # --- Retrieval (with a visible step) ---
    result = RetrievalResult()
    retrieval_failed = False
    _t = time.perf_counter()
    if pinned is not None:
        result = RetrievalResult(chunks=[pinned], top_vector=1.0)
        if not access.is_allowed(pinned, excluded):
            result.restricted_manual = pinned.get("manual")
    else:
        async with cl.Step(name="Searching the manuals…", type="retrieval") as step:
            try:
                result = await retrieve_scored(
                    query, k=8 if prefer_procedures else 5, excluded_manuals=excluded
                )
                if prefer_procedures:
                    procs = [c for c in result.chunks if c.get("kind") == "procedure"]
                    if procs:
                        result.chunks = procs[:5]
                step.output = f"Found {len(result.chunks)} relevant chunk(s)."
            except Exception:
                log.exception("Retrieval failed")
                retrieval_failed = True
                step.output = "Retrieval failed — see logs."
    chunks = result.chunks
    _retrieval_s = time.perf_counter() - _t

    if retrieval_failed:
        await _send_fixed_reply(fallback.ERROR_REPLY, fallback.ERROR, conv_id)
        return

    # --- A task in a manual this user may not be answered from ---
    if result.restricted_manual:
        log.info(
            "⏱ REQUEST TOTAL=%.2fs | restricted (%s) | query=%r",
            time.perf_counter() - _t_req, result.restricted_manual, query[:60],
        )
        await _send_fixed_reply(access.restricted_reply(result.restricted_manual), access.RESTRICTED, conv_id)
        return

    # --- "X is not working": the Focal Person, plus X's steps to re-check ---
    if pinned is None and not prefer_procedures:
        trouble = topics.troubleshooting(query, result)
        if trouble is not None:
            reply, outcome = trouble.reply, fallback.TROUBLESHOOT
            if not trouble.options:
                # No task identified: make sure it is not a clinical, patient-
                # data or off-topic message ("my patient can't breathe").
                label = await classifier.classify_question(query)
                if label in ("CLINICAL", "PATIENT_DATA", "OTHER"):
                    reply, outcome = fallback.fallback_reply(label, [])
            log.info(
                "⏱ REQUEST TOTAL=%.2fs | something not working, offering %d topic(s) -> %s | query=%r",
                time.perf_counter() - _t_req, len(trouble.options), outcome, query[:60],
            )
            await _send_fixed_reply(
                reply, outcome, conv_id,
                chunks=trouble.options, actions=_topic_actions(trouble.options),
            )
            return

    # --- Vague question: ask which topic was meant ---
    if pinned is None and not prefer_procedures:
        asked = topics.clarification(query, result)
        if asked is not None:
            log.info(
                "⏱ REQUEST TOTAL=%.2fs | vague question, offering %d topic(s) | query=%r",
                time.perf_counter() - _t_req, len(asked.options), query[:60],
            )
            await _send_fixed_reply(
                asked.reply, fallback.CLARIFY, conv_id,
                chunks=asked.options, actions=_topic_actions(asked.options),
            )
            return

    # --- Weak match: don't generate, give a fixed reply ---
    # A cross-reference click searches for a workflow the manuals themselves
    # named, so it is always answered.
    if not prefer_procedures and not result.is_strong:
        _t = time.perf_counter()
        reply, outcome = await _classified_reply(query, excluded)
        related: list[dict[str, Any]] = []
        if outcome == fallback.NOT_IN_MANUAL:
            # The Focal Person is still the answer; a close topic may also help.
            related = topics.topic_options(result.candidates, limit=2, min_score=topics.MIN_VECTOR)
            if related:
                reply += "\n\n" + topics.RELATED_TOPICS_INTRO
        log.info(
            "⏱ REQUEST TOTAL=%.2fs | retrieval=%.2fs | classify=%.2fs | weak match (vector=%.3f bm25=%.3f) -> %s | query=%r",
            time.perf_counter() - _t_req, _retrieval_s, time.perf_counter() - _t,
            result.top_vector, result.top_bm25, outcome, query[:60],
        )
        await _send_fixed_reply(reply, outcome, conv_id, chunks=chunks, actions=_topic_actions(related))
        return

    # --- Streaming generation ---
    assistant_msg = cl.Message(content="")
    full_answer = ""
    generation_failed = False
    _t = time.perf_counter()
    _first_token_s: float | None = None
    try:
        async for token in stream_answer(query, chunks):
            if _first_token_s is None:
                _first_token_s = time.perf_counter() - _t
            full_answer += token
            await assistant_msg.stream_token(token)
    except Exception:
        log.exception("Generation failed")
        generation_failed = True
        await assistant_msg.stream_token("\n\n" + fallback.ERROR_REPLY)
        full_answer += "\n\n" + fallback.ERROR_REPLY
    _generation_s = time.perf_counter() - _t

    # --- Post-stream: outcome, captioned screenshots, citation, reference buttons ---
    _t = time.perf_counter()
    outcome = fallback.ERROR if generation_failed else fallback.detect_outcome(full_answer)

    # The manuals carry honest "not yet documented" placeholders. When the
    # answer is one of those, replace the model's paraphrase with the fixed
    # wording so the user is always sent to the Focal Person.
    stub = None if generation_failed else fallback.cited_stub(full_answer, chunks)
    if stub is not None:
        full_answer = fallback.STUB_REPLY.format(title=fallback.stub_title(stub))
        outcome = fallback.NOT_IN_MANUAL
    elif outcome == fallback.CLINICAL:
        full_answer = fallback.CLINICAL_REPLY
    elif outcome == fallback.PATIENT_DATA:
        full_answer = fallback.PATIENT_DATA_REPLY
    elif outcome == fallback.NOT_IN_MANUAL:
        if chunks and fallback.is_stub(chunks[0]):
            # Best match is a placeholder and the model declined: name the
            # feature rather than give the generic "couldn't find this".
            full_answer = fallback.STUB_REPLY.format(title=fallback.stub_title(chunks[0]))
        else:
            # The model found nothing usable in a strong match. Say WHY in the
            # same words a weak match would get: "how many patients are
            # admitted today?" is a patient-data question, not a manual gap.
            full_answer, outcome = await _classified_reply(query, excluded)

    # Screenshots, the citation and cross-reference buttons belong to a real
    # answer only — never under a decline or an error.
    actions: list[cl.Action] = []
    if outcome == fallback.ANSWERED and chunks and full_answer:
        chunks_used = _chunks_referenced_by(full_answer, chunks)
        # Cautions and Notes must be the manual's own (app/rag/callouts.py).
        full_answer = callouts.check_callouts(full_answer, chunks_used)
        full_answer = callouts.add_missing_callouts(full_answer, chunks_used[0] if chunks_used else None)
        full_answer = callouts.focal_person_wording(full_answer)
        citation = ""
        if "source:" not in full_answer.lower():
            cite_chunk = chunks_used[0] if chunks_used else chunks[0]
            citation = _citation(cite_chunk) or ""
        # Each screenshot goes under the step it illustrates; any that can't
        # be matched to a step are listed after the answer (app/ui/figures.py).
        full_answer = answer_with_figures(full_answer, chunks_used) + citation
        actions = _reference_actions(chunks_used)

    assistant_msg.content = full_answer
    assistant_msg.elements = []
    assistant_msg.actions = actions
    await assistant_msg.update()
    _postprocess_s = time.perf_counter() - _t

    # --- Timing summary (perf/latency-profiling branch) ---
    _total_s = time.perf_counter() - _t_req
    log.info(
        "⏱ REQUEST TOTAL=%.2fs | retrieval=%.2fs | gen_first_token=%.2fs | gen_total=%.2fs | postprocess=%.2fs | outcome=%s | query=%r",
        _total_s, _retrieval_s, (_first_token_s or 0.0), _generation_s, _postprocess_s, outcome, query[:60],
    )

    # --- Persist assistant turn ---
    if conv_id is not None:
        try:
            await queries.record_message(
                conversation_id=conv_id,
                role="assistant",
                content=full_answer,
                retrieved_chunk_ids=[c["id"] for c in chunks],
                outcome=outcome,
            )
        except Exception:
            log.exception("Failed to persist assistant message")


async def _classified_reply(query: str, excluded: list[str]) -> tuple[str, str]:
    """Classify a question the manuals can't answer -> (fixed reply, outcome)."""
    label = await classifier.classify_question(query)
    modules: list[str] = []
    hidden: list[str] = []
    if label == classifier.ASSISTANT:
        try:
            modules, hidden = access.visible_modules(await queries.list_document_titles(), excluded)
        except Exception:
            log.exception("Could not list manuals for the 'about' reply")
    reply, outcome = fallback.fallback_reply(label, modules)
    if outcome == fallback.ABOUT_ASSISTANT:
        reply += access.restricted_modules_line(hidden)
    return reply, outcome


async def _send_fixed_reply(
    reply: str,
    outcome: str,
    conv_id: UUID | None,
    *,
    chunks: list[dict[str, Any]] | None = None,
    actions: list[cl.Action] | None = None,
) -> None:
    """Send one of the fixed replies and persist it with its outcome."""
    await cl.Message(content=reply, actions=actions or []).send()
    if conv_id is None:
        return
    try:
        await queries.record_message(
            conversation_id=conv_id,
            role="assistant",
            content=reply,
            retrieved_chunk_ids=[c["id"] for c in (chunks or [])],
            outcome=outcome,
        )
    except Exception:
        log.exception("Failed to persist assistant message")


@cl.on_message
async def on_message(message: cl.Message) -> None:
    user_query = (message.content or "").strip()
    if not user_query:
        return

    account, excluded = await _gate()
    if account is None:
        return

    conv_id: UUID | None = cl.user_session.get("conversation_id")
    if conv_id is None:
        # Edge case: on_chat_start didn't run (race / refresh). Recover.
        conv_id = await queries.create_conversation(user_id=account["username"])
        cl.user_session.set("conversation_id", conv_id)

    # Persist the user turn now so it's recoverable even if generation fails.
    await queries.record_message(
        conversation_id=conv_id,
        role="user",
        content=user_query,
    )

    # --- Short-circuit conversational filler -----------------------------
    if fallback.is_chitchat(user_query):
        await _send_fixed_reply(_FILLER_REPLY, fallback.FILLER, conv_id)
        return

    await _respond(user_query, conv_id, excluded=excluded)


@cl.action_callback("open_reference")
async def on_open_reference(action: cl.Action) -> None:
    """A cross-reference button was clicked: re-run retrieval for the referenced
    workflow and post it as a fresh answer (with its own screenshots/buttons)."""
    query = ((action.payload or {}).get("query") or "").strip()
    if not query:
        return
    account, excluded = await _gate()
    if account is None:
        return
    conv_id: UUID | None = cl.user_session.get("conversation_id")
    if conv_id is not None:
        try:
            await queries.record_message(
                conversation_id=conv_id, role="user", content=f"(opened reference) {query}",
            )
        except Exception:
            log.exception("Failed to persist reference-click turn")
    await cl.Message(content=f"📄 Opening **{query}**…").send()
    await _respond(query, conv_id, excluded=excluded, prefer_procedures=True)


@cl.action_callback("open_topic")
async def on_open_topic(action: cl.Action) -> None:
    """A topic was chosen from a list: answer from exactly that procedure."""
    payload = action.payload or {}
    title = (payload.get("title") or "").strip()
    if not title:
        return
    account, excluded = await _gate()
    if account is None:
        return
    conv_id: UUID | None = cl.user_session.get("conversation_id")
    if conv_id is not None:
        try:
            await queries.record_message(
                conversation_id=conv_id, role="user", content=f"(chose topic) {title}",
            )
        except Exception:
            log.exception("Failed to persist topic-click turn")
    await cl.Message(content=f"📄 Opening **{title}**…").send()

    chunk: dict[str, Any] | None = None
    try:
        rows = await queries.get_chunks_by_ids([UUID(payload.get("chunk_id"))])
        chunk = rows[0] if rows else None
    except Exception:
        log.exception("Could not load the chosen topic; searching by its title instead")
    if chunk is not None:
        await _respond(title, conv_id, excluded=excluded, pinned=chunk)
    else:
        await _respond(title, conv_id, excluded=excluded, prefer_procedures=True)
