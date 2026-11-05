"""Chainlit entry point for the eAFYA Assistant.

The Phase 5 wiring:
    on_chat_start
        - read X-User header (set by Caddy basic-auth)
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

Header auth: Caddy validates basic-auth and forwards X-User. We treat that
as the user identity. If the header isn't present (dev without auth, or
auth disabled), we fall back to "anonymous".
"""
from __future__ import annotations

import logging
import re
from typing import Any
from uuid import UUID

import chainlit as cl

from app.config import settings
from app.db import queries
from app.rag import classifier, fallback, reranker
from app.rag.generator import stream_answer
from app.rag.retriever import RetrievalResult, retrieve_scored
from app.ui.figures import answer_with_figures
from app.ui.starters import get_starters

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
        _fastapi_app.router.routes.insert(
            0,
            Mount(
                settings.images_url_prefix,
                app=StaticFiles(directory=str(settings.images_dir)),
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


# ---------------------------------------------------------------------------
# Authentication.
#
# DISABLED: the Chainlit header-auth callback below made Chainlit show its own
# /login page and call POST /auth/header from the browser. Behind Caddy basic
# auth this loops: the browser does not attach the cached basic-auth header to
# those background fetches, so Caddy 401s them and Chainlit bounces back to
# /login forever (confirmed in the Caddy access logs — /auth/header without an
# Authorization header returns 401).
#
# Access is already gated by Caddy basic auth (username admin) in front of the
# whole app, so we let Chainlit run WITHOUT its own auth layer: pass the native
# basic-auth dialog once and you are straight into the chat. Without an auth
# callback registered, on_chat_start's `cl.user_session.get("user")` is None and
# user_id falls back to "anonymous" (already handled below).
#
# To restore per-user identity later, prefer a single auth mechanism (e.g.
# Chainlit OAuth/password auth, OR read X-User in on_chat_start) rather than
# stacking Chainlit header-auth on top of Caddy basic-auth.
# ---------------------------------------------------------------------------

# @cl.header_auth_callback
# def header_auth(headers) -> cl.User | None:
#     user_id = (headers.get("x-user") or headers.get("X-User") or "anonymous").strip()
#     return cl.User(identifier=user_id, metadata={"source": "header"})


# ---------------------------------------------------------------------------
# Chat lifecycle
# ---------------------------------------------------------------------------

@cl.set_starters
async def set_starters():
    return get_starters()


@cl.on_chat_start
async def on_chat_start() -> None:
    user = cl.user_session.get("user")
    user_id = user.identifier if isinstance(user, cl.User) else "anonymous"

    conv_id = await queries.create_conversation(user_id=user_id)
    cl.user_session.set("conversation_id", conv_id)

    log.info("on_chat_start: user=%s, conversation=%s", user_id, conv_id)

    await cl.Message(
        content=(
            "👋 Hi! I'm the **eAFYA Assistant** — your guide to eAFYA, the "
            "Ministry of Health EMR system.\n\n"
            "Ask me how to do anything from the user manuals — registering a "
            "patient, queueing them for a doctor, closing a clinic session, "
            "scheduling reminders, generating reports — and I'll walk you "
            "through the steps with the **screenshots** from the manual.\n\n"
            "ℹ️ I **don't** read patient data and I **don't** give clinical "
            "advice. For clinical questions, please consult a clinician or "
            "your clinical reference. For anything the manuals don't cover, "
            f"contact your facility's **{fallback.FOCAL_PERSON}**.\n\n"
            "Try one of the suggestions below, or ask anything in your own words."
        ),
    ).send()


# ---------------------------------------------------------------------------
# Per-message handler
# ---------------------------------------------------------------------------

# The chunk-attribution heuristic: which retrieved chunks did the LLM actually
# answer from? We approximate by checking whether the chunk's title (or any
# substantial bold UI label) appears as a substring of the answer text.
_LABEL_TOKEN_RE = re.compile(r"\S+")


# Short, conversational, or pure-filler messages should not trigger retrieval.
# Without this guard, typing "okay" sends "okay" through vector + BM25 + trigram
# and produces a confident-looking answer drawn from whichever chunk happened
# to top-rank. That's a confusing UX failure.
_FILLER_PHRASES = {
    "hi", "hello", "hey", "hey there", "yo",
    "ok", "okay", "k", "kk", "sure", "right", "alright", "got it", "cool", "nice",
    "yes", "yep", "yeah", "yup",
    "no", "nope",
    "thanks", "thank you", "thx", "ty", "cheers",
    "bye", "goodbye",
    "test", "testing",
    "?",
}


def _is_filler_query(query: str) -> bool:
    """Return True if the message doesn't look like a real question.

    Heuristic: lowercased + stripped punctuation matches a known filler phrase,
    OR the message is very short (fewer than 3 words and fewer than 12 chars).
    """
    q = query.lower().strip().rstrip("?!.,")
    if not q:
        return True
    if q in _FILLER_PHRASES:
        return True
    if len(q.split()) < 3 and len(q) < 12:
        return True
    return False


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

    Only 'procedure' chunks carry screenshots; 'index_entry' / 'glossary' /
    'section_intro' chunks are text-only. A "How do I X?" query often ranks the
    Quick-Index entry (same title, 0 images) above the real procedure, so we
    must PREFER procedures here — otherwise the answer shows the steps but no
    screenshots (and cites the index instead of the procedure).

    Order of preference:
      1. procedure chunks whose title appears in the answer,
      2. the highest-ranked procedure chunk retrieved (model may have paraphrased),
      3. any chunk whose title appears in the answer,
      4. the top-ranked chunk.
    """
    if not chunks:
        return []
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


async def _respond(query: str, conv_id: UUID | None, *, prefer_procedures: bool = False) -> None:
    """Retrieve → stream an answer → append captioned screenshots, a citation,
    and clickable cross-reference buttons → persist. Shared by on_message and
    the cross-reference action callback so both behave identically.

    prefer_procedures: set True for cross-reference clicks. A "See module: X"
    click searches a broad module name, which can match the module's text-only
    Quick Index / overview (no screenshots). Preferring procedure-kind chunks
    makes the opened workflow land on a real procedure that carries its images.
    """
    import time  # perf profiling (perf/latency-profiling branch)
    _t_req = time.perf_counter()

    # --- Retrieval (with a visible step) ---
    result = RetrievalResult()
    retrieval_failed = False
    _t = time.perf_counter()
    async with cl.Step(name="Searching the manuals…", type="retrieval") as step:
        try:
            result = await retrieve_scored(query, k=8 if prefer_procedures else 5)
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

    # --- Weak match: don't generate, give a fixed reply ---
    # A cross-reference click searches for a workflow the manuals themselves
    # named, so it is always answered.
    if not prefer_procedures and not result.is_strong:
        _t = time.perf_counter()
        reply, outcome = await _classified_reply(query)
        log.info(
            "⏱ REQUEST TOTAL=%.2fs | retrieval=%.2fs | classify=%.2fs | weak match (vector=%.3f bm25=%.3f) -> %s | query=%r",
            time.perf_counter() - _t_req, _retrieval_s, time.perf_counter() - _t,
            result.top_vector, result.top_bm25, outcome, query[:60],
        )
        await _send_fixed_reply(reply, outcome, conv_id, chunks=chunks)
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
            full_answer, outcome = await _classified_reply(query)

    # Screenshots, the citation and cross-reference buttons belong to a real
    # answer only — never under a decline or an error.
    actions: list[cl.Action] = []
    if outcome == fallback.ANSWERED and chunks and full_answer:
        chunks_used = _chunks_referenced_by(full_answer, chunks)
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


async def _classified_reply(query: str) -> tuple[str, str]:
    """Classify a question the manuals can't answer -> (fixed reply, outcome)."""
    label = await classifier.classify_question(query)
    modules: list[str] = []
    if label == classifier.ASSISTANT:
        try:
            modules = await queries.list_document_titles()
        except Exception:
            log.exception("Could not list manuals for the 'about' reply")
    return fallback.fallback_reply(label, modules)


async def _send_fixed_reply(
    reply: str,
    outcome: str,
    conv_id: UUID | None,
    *,
    chunks: list[dict[str, Any]] | None = None,
) -> None:
    """Send one of the fixed fallback.py replies and persist it with its outcome."""
    await cl.Message(content=reply).send()
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

    conv_id: UUID | None = cl.user_session.get("conversation_id")
    if conv_id is None:
        # Edge case: on_chat_start didn't run (race / refresh). Recover.
        user = cl.user_session.get("user")
        user_id = user.identifier if isinstance(user, cl.User) else "anonymous"
        conv_id = await queries.create_conversation(user_id=user_id)
        cl.user_session.set("conversation_id", conv_id)

    # Persist the user turn now so it's recoverable even if generation fails.
    await queries.record_message(
        conversation_id=conv_id,
        role="user",
        content=user_query,
    )

    # --- Short-circuit conversational filler -----------------------------
    if _is_filler_query(user_query):
        await _send_fixed_reply(_FILLER_REPLY, fallback.FILLER, conv_id)
        return

    await _respond(user_query, conv_id)


@cl.action_callback("open_reference")
async def on_open_reference(action: cl.Action) -> None:
    """A cross-reference button was clicked: re-run retrieval for the referenced
    workflow and post it as a fresh answer (with its own screenshots/buttons)."""
    query = ((action.payload or {}).get("query") or "").strip()
    if not query:
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
    await _respond(query, conv_id, prefer_procedures=True)
