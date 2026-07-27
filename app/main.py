"""Chainlit entry point for EMR Helper.

The Phase 5 wiring:
    on_chat_start
        - read X-User header (set by Caddy basic-auth)
        - create a conversations row for this session
        - greet + show starters
    on_message
        - show a "Searching the manuals…" step
        - retrieve top-5 chunks
        - persist the user turn
        - stream the LLM answer into a Chainlit message
        - after streaming, attach images from chunks the answer actually
          drew on, append a citation footer, persist the assistant turn

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
from app.rag import reranker
from app.rag.generator import stream_answer
from app.rag.retriever import retrieve
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
# Header auth — Caddy passes X-User after basic auth succeeds.
# ---------------------------------------------------------------------------

@cl.header_auth_callback
def header_auth(headers) -> cl.User | None:
    """Authenticate by trusting the X-User header set by Caddy.

    Returns a cl.User for any non-empty header value (or 'anonymous' if
    absent — useful in dev when accessing without the proxy). Returning
    None would block the user from connecting; we don't want that.

    Note: this is `def`, not `async def` — Chainlit calls header auth
    synchronously during the websocket handshake.
    """
    # Chainlit normalises headers to lowercase keys.
    user_id = (headers.get("x-user") or headers.get("X-User") or "anonymous").strip()
    return cl.User(identifier=user_id, metadata={"source": "header"})


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
            "👋 Hi! I'm **EMR Helper** — your guide to the Ministry of Health "
            "EMR system.\n\n"
            "Ask me how to do anything from the user manuals — registering a "
            "patient, queueing them for a doctor, closing a clinic session, "
            "scheduling reminders, generating reports — and I'll walk you "
            "through the steps with the **screenshots** from the manual.\n\n"
            "ℹ️ I **don't** read patient data and I **don't** give clinical "
            "advice. For clinical questions, please consult a clinician or "
            "your clinical reference.\n\n"
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
    "Hello! 👋 I'm **EMR Helper** — I answer questions about how to use the "
    "EMR system from the official user manuals.\n\n"
    "Try something specific, for example:\n"
    "- *How do I register a new patient?*\n"
    "- *How do I close a clinic session?*\n"
    "- *How do I schedule an appointment reminder?*"
)


def _chunks_referenced_by(answer: str, chunks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Return the chunks whose title (case-insensitive) appears in `answer`.

    Falls back to the top-1 chunk if no titles match — there are nearly
    always *some* images to show, and if the model paraphrased the title
    we still want its screenshots visible.
    """
    if not chunks:
        return []
    answer_lower = answer.lower()
    matched = [c for c in chunks if c.get("title") and c["title"].lower() in answer_lower]
    if matched:
        return matched
    return [chunks[0]]


def _image_url(raw: str) -> str:
    """Convert a stored image path (…/data/images/<slug>/fig_NN.png) into the
    URL served by the static mount (e.g. /images/<slug>/fig_NN.png)."""
    p = str(raw).replace("\\", "/")
    root = str(settings.images_dir).replace("\\", "/").rstrip("/")
    prefix = settings.images_url_prefix.rstrip("/")
    if p.startswith(root):
        return prefix + p[len(root):]
    # Fallback: keep the last two components (<slug>/<file>).
    parts = [seg for seg in p.split("/") if seg]
    if len(parts) >= 2:
        return f"{prefix}/" + "/".join(parts[-2:])
    return p


def _build_figures_markdown(chunks_used: list[dict[str, Any]]) -> str:
    """Build a Markdown block that shows each screenshot under its own caption,
    in document order. Embedding the images as Markdown (rather than a bottom
    gallery of unlabelled thumbnails) keeps each figure next to the caption that
    says what it shows, so readers can map a screenshot to the step it matches.
    """
    seen_paths: set[str] = set()
    items: list[tuple[int, str, str]] = []  # (order, caption, url)
    for c in chunks_used:
        images = c.get("images") or []
        if isinstance(images, str):
            try:
                import json as _json
                images = _json.loads(images)
            except Exception:
                log.warning("Chunk %s has unparseable images jsonb; skipping", c.get("id"))
                continue
        for img in images:
            if not isinstance(img, dict):
                continue
            path = img.get("path", "")
            if not path or path in seen_paths:
                continue
            seen_paths.add(path)
            caption = (img.get("caption") or f"Figure {img.get('order', '?')}").strip()
            items.append((int(img.get("order", 0) or 0), caption, _image_url(path)))

    if not items:
        return ""

    items.sort(key=lambda t: t[0])  # document order
    lines = ["\n\n---\n\n**Screenshots**\n"]
    for _order, caption, url in items:
        # Caption first (so it labels the figure), then the full image beneath it.
        alt = caption.replace("]", ")").replace("[", "(")  # keep Markdown alt-text safe
        lines.append(f"\n*{caption}*\n\n![{alt}]({url})\n")
    log.info("Built figures markdown with %d image(s) from %d chunk(s)", len(items), len(chunks_used))
    return "\n".join(lines)


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
    # --- Retrieval (with a visible step) ---
    chunks: list[dict[str, Any]] = []
    async with cl.Step(name="Searching the manuals…", type="retrieval") as step:
        try:
            chunks = await retrieve(query, k=8 if prefer_procedures else 5)
            if prefer_procedures:
                procs = [c for c in chunks if c.get("kind") == "procedure"]
                if procs:
                    chunks = procs[:5]
            step.output = f"Found {len(chunks)} relevant chunk(s)."
        except Exception:
            log.exception("Retrieval failed")
            step.output = "Retrieval failed — see logs."

    # --- Streaming generation ---
    assistant_msg = cl.Message(content="")
    full_answer = ""
    try:
        async for token in stream_answer(query, chunks):
            full_answer += token
            await assistant_msg.stream_token(token)
    except Exception:
        log.exception("Generation failed")
        fallback = (
            "\n\nSomething went wrong while generating an answer. "
            "Please try again — and if it keeps happening, ask your administrator "
            "to check the Ollama service."
        )
        await assistant_msg.stream_token(fallback)
        full_answer += fallback

    # --- Post-stream: captioned screenshots, citation, reference buttons ---
    chunks_used = _chunks_referenced_by(full_answer, chunks)
    figures_md = _build_figures_markdown(chunks_used)

    answer_lower = full_answer.lower()
    citation = ""
    if (
        chunks
        and full_answer
        and "i don't have that in the manuals" not in answer_lower
        and "source:" not in answer_lower
    ):
        cite_chunk = chunks_used[0] if chunks_used else chunks[0]
        citation = _citation(cite_chunk) or ""

    full_answer = full_answer + figures_md + citation
    assistant_msg.content = full_answer
    assistant_msg.elements = []
    assistant_msg.actions = _reference_actions(chunks_used)
    await assistant_msg.update()

    # --- Persist assistant turn ---
    if conv_id is not None:
        try:
            await queries.record_message(
                conversation_id=conv_id,
                role="assistant",
                content=full_answer,
                retrieved_chunk_ids=[c["id"] for c in chunks],
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
        await cl.Message(content=_FILLER_REPLY).send()
        await queries.record_message(
            conversation_id=conv_id,
            role="assistant",
            content=_FILLER_REPLY,
            retrieved_chunk_ids=[],
        )
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
