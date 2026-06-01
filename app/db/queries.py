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

import json
from typing import Any
from uuid import UUID

import asyncpg

from app.db.pool import get_pool


# ---------------------------------------------------------------------------
# Documents (Phase 3)
# ---------------------------------------------------------------------------

async def insert_document(
    *,
    title: str,
    manual_version: str,
    source_path: str,
    conn: asyncpg.Connection | None = None,
) -> UUID:
    """Insert a document row, or return the existing id for the same
    (title, manual_version). Idempotent — safe to call on every ingest run.

    If `conn` is provided, the query runs on that connection (so the caller
    can wrap insert+upserts in one transaction). Otherwise the pool is used.
    """
    sql = """
        INSERT INTO documents (title, manual_version, source_path)
        VALUES ($1, $2, $3)
        ON CONFLICT (title, manual_version) DO UPDATE
            SET source_path = EXCLUDED.source_path,
                ingested_at = now()
        RETURNING id
    """

    async def run(c: asyncpg.Connection) -> UUID:
        return await c.fetchval(sql, title, manual_version, source_path)

    if conn is not None:
        return await run(conn)
    pool = await get_pool()
    async with pool.acquire() as c:
        return await run(c)


async def mark_document_chunks_inactive(
    doc_id: UUID,
    conn: asyncpg.Connection | None = None,
) -> int:
    """Mark every chunk belonging to a document as `active=false`. Called
    at the start of a re-ingest before inserting fresh chunks. Returns the
    number of rows affected.

    Old chunks are *not* deleted — they may still be referenced by past
    conversations, and historical answers must remain reconstructible.
    """
    sql = "UPDATE chunks SET active = false WHERE doc_id = $1 AND active = true"

    async def run(c: asyncpg.Connection) -> int:
        status = await c.execute(sql, doc_id)
        # status format: "UPDATE <n>"
        try:
            return int(status.split()[-1])
        except (ValueError, IndexError):
            return 0

    if conn is not None:
        return await run(conn)
    pool = await get_pool()
    async with pool.acquire() as c:
        return await run(c)


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
    conn: asyncpg.Connection | None = None,
) -> UUID:
    """Insert a single chunk row. Returns the new chunk's id.

    "Upsert" is a slight misnomer: chunk identity is not stable across
    re-ingests (chunking is heuristic). The re-ingest flow is "mark old
    inactive, insert new" — this function does only the INSERT half.
    """
    sql = """
        INSERT INTO chunks (
            doc_id, kind, section_path, title, when_to_use, content,
            image_captions, images, ui_labels, cautions, notes,
            page_start, page_end, manual_version, embedding
        )
        VALUES (
            $1, $2, $3, $4, $5, $6,
            $7, $8::jsonb, $9, $10, $11,
            $12, $13, $14, $15
        )
        RETURNING id
    """

    async def run(c: asyncpg.Connection) -> UUID:
        return await c.fetchval(
            sql,
            doc_id, kind, section_path, title, when_to_use, content,
            image_captions, json.dumps(images), ui_labels, cautions, notes,
            page_start, page_end, manual_version, embedding,
        )

    if conn is not None:
        return await run(conn)
    pool = await get_pool()
    async with pool.acquire() as c:
        return await run(c)


# ---------------------------------------------------------------------------
# Retrieval (Phase 4)
# ---------------------------------------------------------------------------

# SQL for the three parallel search lanes. They share the same WHERE filter
# (active=true, manual_version restriction) and differ only in scoring +
# ordering. Each returns (id, score) tuples in score-descending order.
#
# Notes on operators / functions used:
#   embedding <=> $1::vector   pgvector cosine distance. ORDER BY ascending
#                              gives most-similar first (distance 0 = identical).
#                              We return 1 - distance as the "score" for RRF.
#   plainto_tsquery('english') Safe parse of a user query into a tsquery
#                              (handles punctuation, no error on stop words).
#   ts_rank_cd(tsv, query)     BM25-style ranking; cd = cover-density variant
#                              (weights matches by token distance).
#   similarity(ui_labels, $1)  pg_trgm trigram similarity. We don't use the %
#                              filter operator because some queries match
#                              labels below the default threshold but should
#                              still rank — we just take the top-N by similarity.

_VECTOR_SQL = """
    SELECT id, 1.0 - (embedding <=> $1::vector) AS score
    FROM chunks
    WHERE active = true
      AND ($2::text[] IS NULL OR manual_version = ANY($2))
      AND embedding IS NOT NULL
    ORDER BY embedding <=> $1::vector
    LIMIT $3
"""

_BM25_SQL = """
    SELECT id, ts_rank_cd(tsv, q) AS score
    FROM chunks, plainto_tsquery('english', $1) AS q
    WHERE active = true
      AND ($2::text[] IS NULL OR manual_version = ANY($2))
      AND tsv @@ q
    ORDER BY score DESC
    LIMIT $3
"""

_TRIGRAM_SQL = """
    SELECT id, similarity(ui_labels, $1) AS score
    FROM chunks
    WHERE active = true
      AND ($2::text[] IS NULL OR manual_version = ANY($2))
      AND ui_labels <> ''
      AND similarity(ui_labels, $1) > 0.0
    ORDER BY score DESC
    LIMIT $3
"""


async def hybrid_search(
    *,
    query_text: str,
    query_vec: list[float],
    manual_versions: list[str] | None = None,
    k_vector: int = 50,
    k_bm25: int = 50,
    k_trigram: int = 30,
) -> dict[str, list[tuple[UUID, float]]]:
    """Run three parallel searches against the chunks table.

    Returns a dict with keys 'vector', 'bm25', 'trigram', each mapped to a
    list of (chunk_id, score) tuples in score-descending order. RRF fusion
    happens one level up in app/rag/retriever.py.

    manual_versions=None means "search all versions" (only sensible when
    one version is loaded). Pass a list to scope to specific versions.
    """
    pool = await get_pool()
    async with pool.acquire() as conn:
        # Run the three queries concurrently. asyncpg pipelines them on the
        # same connection serially internally, but we still avoid network
        # round-trip stalls by not awaiting each before issuing the next.
        # For simplicity here we run sequentially — the entire search is
        # comfortably under 100 ms on a small corpus, so concurrency adds
        # complexity without measurable benefit.
        vec_rows = await conn.fetch(
            _VECTOR_SQL, query_vec, manual_versions, k_vector
        )
        bm25_rows = await conn.fetch(
            _BM25_SQL, query_text, manual_versions, k_bm25
        )
        trgm_rows = await conn.fetch(
            _TRIGRAM_SQL, query_text, manual_versions, k_trigram
        )

    return {
        "vector":  [(r["id"], float(r["score"])) for r in vec_rows],
        "bm25":    [(r["id"], float(r["score"])) for r in bm25_rows],
        "trigram": [(r["id"], float(r["score"])) for r in trgm_rows],
    }


async def get_chunks_by_ids(chunk_ids: list[UUID]) -> list[dict[str, Any]]:
    """Fetch full chunk rows by id, preserving the input order.

    Postgres returns rows in arbitrary order for ANY($1), so we re-order
    on the Python side. Empty input returns empty list.

    NOTE: asyncpg returns `jsonb` columns as raw JSON strings by default
    (not parsed). We decode `images` here so downstream code can treat it
    as a list of dicts. If we ever add more jsonb columns to chunks, decode
    them here too.
    """
    if not chunk_ids:
        return []

    sql = """
        SELECT id, doc_id, kind, section_path, title, when_to_use, content,
               image_captions, images, ui_labels, cautions, notes,
               page_start, page_end, manual_version, active, created_at
        FROM chunks
        WHERE id = ANY($1)
    """
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(sql, chunk_ids)

    by_id: dict[UUID, dict[str, Any]] = {}
    for r in rows:
        d = dict(r)
        # asyncpg returns jsonb as a raw JSON str -- parse so callers can
        # iterate `images` as a list of dicts and read img["path"], etc.
        images = d.get("images")
        if isinstance(images, str):
            try:
                d["images"] = json.loads(images)
            except json.JSONDecodeError:
                d["images"] = []
        elif images is None:
            d["images"] = []
        by_id[r["id"]] = d

    return [by_id[cid] for cid in chunk_ids if cid in by_id]


# ---------------------------------------------------------------------------
# Chat persistence (Phase 5)
# ---------------------------------------------------------------------------

async def create_conversation(user_id: str) -> UUID:
    """Create a fresh conversation row and return its id.

    For now every browser session gets a new conversation (Chainlit's
    @cl.on_chat_start fires it). A later phase can implement "resume the
    most recent conversation within N minutes" if users want continuity.
    """
    sql = """
        INSERT INTO conversations (user_id)
        VALUES ($1)
        RETURNING id
    """
    pool = await get_pool()
    async with pool.acquire() as conn:
        return await conn.fetchval(sql, user_id)


async def record_message(
    *,
    conversation_id: UUID,
    role: str,
    content: str,
    retrieved_chunk_ids: list[UUID] | None = None,
) -> UUID:
    """Persist a user or assistant message. Returns the new message id.

    Also bumps the parent conversation's last_message_at so we can sort
    conversations by recency in any future admin UI.
    """
    chunk_ids_json = json.dumps([str(cid) for cid in (retrieved_chunk_ids or [])])
    sql = """
        WITH new_msg AS (
            INSERT INTO messages (conversation_id, role, content, retrieved_chunk_ids)
            VALUES ($1, $2, $3, $4::jsonb)
            RETURNING id
        ),
        touch AS (
            UPDATE conversations
            SET last_message_at = now()
            WHERE id = $1
        )
        SELECT id FROM new_msg
    """
    pool = await get_pool()
    async with pool.acquire() as conn:
        return await conn.fetchval(sql, conversation_id, role, content, chunk_ids_json)


async def record_feedback(
    *,
    message_id: UUID,
    rating: str,
    comment: str | None = None,
) -> UUID:
    """Record a thumbs-up or thumbs-down on an assistant message.

    Multiple rows per message are allowed (latest wins in any aggregation).
    """
    sql = """
        INSERT INTO feedback (message_id, rating, comment)
        VALUES ($1, $2, $3)
        RETURNING id
    """
    pool = await get_pool()
    async with pool.acquire() as conn:
        return await conn.fetchval(sql, message_id, rating, comment)


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
