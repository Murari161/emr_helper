"""Parameterised SQL for all DB access.

Every function in this module is the *only* place that should construct SQL
for its respective table. Modules elsewhere import these functions; nobody
else writes raw queries.

Phase 2 ships stubs; Phases 3-6 fill them in:
  - insert_document, upsert_chunk     -> Phase 3 (ingestion)
  - hybrid_search, get_chunks_by_ids  -> Phase 4 (retrieval)
  - record_message, record_feedback   -> Phase 5 (chat persistence)
  - log_audit                         -> Phase 6 (audit logging)

Conventions:
  - All functions are `async def` and acquire a connection from the shared pool.
  - Vector inputs come in as Python lists of float; pgvector codec handles serialization.
  - Errors propagate; callers decide whether to retry or surface to the user.
"""
from __future__ import annotations

from typing import Any
from uuid import UUID

from app.db.pool import get_pool


# ---------------------------------------------------------------------------
# Documents (Phase 3)
# ---------------------------------------------------------------------------

async def insert_document(
    *,
    title: str,
    manual_version: str,
    source_path: str,
) -> UUID:
    """Insert a new document row, or return the existing id for the same
    (title, manual_version). Used at the start of an ingest run.

    Phase 3 will implement. For now, raises NotImplementedError.
    """
    raise NotImplementedError("Filled in during Phase 3 (ingestion).")


async def mark_document_chunks_inactive(doc_id: UUID) -> int:
    """Mark all chunks belonging to a document as `active=false`. Called at
    the start of a re-ingest, before inserting fresh chunks for the same
    (title, manual_version). Returns the number of rows updated.

    Phase 3 will implement.
    """
    raise NotImplementedError("Filled in during Phase 3 (ingestion).")


# ---------------------------------------------------------------------------
# Chunks (Phase 3)
# ---------------------------------------------------------------------------

async def upsert_chunk(
    *,
    doc_id: UUID,
    kind: str,
    section_path: str,
    title: str,
    when_to_use: str | None,
    content: str,
    image_captions: str,
    images: list[dict],
    ui_labels: str,
    cautions: str,
    notes: str,
    page_start: int | None,
    page_end: int | None,
    manual_version: str,
    embedding: list[float],
) -> UUID:
    """Insert a single chunk. Returns the new chunk id.

    "Upsert" is a slight misnomer: ingestion never updates a chunk in place
    (chunk identity isn't stable across re-ingests because chunking is heuristic).
    Re-ingest flow is "mark old inactive, insert new". This function does
    only the INSERT half.

    Phase 3 will implement.
    """
    raise NotImplementedError("Filled in during Phase 3 (ingestion).")


# ---------------------------------------------------------------------------
# Retrieval (Phase 4)
# ---------------------------------------------------------------------------

async def hybrid_search(
    *,
    query_text: str,
    query_vec: list[float],
    manual_versions: list[str] | None = None,
    k_vector: int = 50,
    k_bm25: int = 50,
    k_trigram: int = 30,
) -> dict[str, list[tuple[UUID, float]]]:
    """Run three parallel searches against the chunks table and return the
    raw ranked candidate lists for fusion.

    Returns a dict with keys 'vector', 'bm25', 'trigram', each mapped to a
    list of (chunk_id, score) tuples in score-descending order.

    Phase 4 will implement (with RRF fusion happening one level up in
    `app/rag/retriever.py`).
    """
    raise NotImplementedError("Filled in during Phase 4 (retrieval).")


async def get_chunks_by_ids(chunk_ids: list[UUID]) -> list[dict[str, Any]]:
    """Fetch full chunk rows by id, preserving the input order so the
    retriever can hand them to the reranker/generator without re-sorting.

    Phase 4 will implement.
    """
    raise NotImplementedError("Filled in during Phase 4 (retrieval).")


# ---------------------------------------------------------------------------
# Chat persistence (Phase 5)
# ---------------------------------------------------------------------------

async def get_or_create_conversation(user_id: str) -> UUID:
    """Return an open conversation id for the user, creating one if none active.

    Phase 5 will implement.
    """
    raise NotImplementedError("Filled in during Phase 5 (chat surface).")


async def record_message(
    *,
    conversation_id: UUID,
    role: str,
    content: str,
    retrieved_chunk_ids: list[UUID] | None = None,
) -> UUID:
    """Persist a user or assistant message. Returns the new message id.

    Phase 5 will implement.
    """
    raise NotImplementedError("Filled in during Phase 5 (chat surface).")


async def record_feedback(
    *,
    message_id: UUID,
    rating: str,
    comment: str | None = None,
) -> UUID:
    """Record a thumbs-up or thumbs-down on an assistant message.

    Phase 5 will implement.
    """
    raise NotImplementedError("Filled in during Phase 5 (chat surface).")


# ---------------------------------------------------------------------------
# Audit log (Phase 6)
# ---------------------------------------------------------------------------

async def log_audit(
    *,
    user_id_hash: str,
    event_type: str,
    payload: dict[str, Any],
) -> None:
    """Write one audit row. user_id_hash is the first 16 hex chars of
    sha256(raw_user_id); raw user IDs MUST NOT reach this function.

    Phase 6 will implement (with a hashing helper in app/util.py).
    """
    raise NotImplementedError("Filled in during Phase 6 (eval + audit).")
