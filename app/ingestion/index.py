"""Embed chunks via Ollama and insert them into Postgres.

This is the last step in the ingestion pipeline. By the time this runs:
  - load.py has parsed the .docx into elements
  - images.py has written image binaries to disk
  - chunk.py has produced a list of Chunks ready for the DB

This module talks to Ollama over HTTP (no SDK — just `httpx`) and to
Postgres via the shared asyncpg pool. Each document is processed in a
single transaction: if any chunk fails to insert, the whole document is
rolled back so we never end up with a partially-ingested manual.
"""
from __future__ import annotations

import logging
from uuid import UUID

import httpx

from app.config import settings
from app.db import queries
from app.db.pool import get_pool
from app.ingestion.chunk import Chunk

log = logging.getLogger(__name__)


async def embed_text(text: str, client: httpx.AsyncClient | None = None) -> list[float]:
    """Call Ollama's /api/embeddings endpoint and return the embedding vector.

    Errors propagate. The caller decides whether to retry or fail the ingest.
    """
    payload = {"model": settings.embedding_model, "prompt": text}

    async def _call(c: httpx.AsyncClient) -> list[float]:
        resp = await c.post(
            f"{settings.ollama_base_url}/api/embeddings",
            json=payload,
            timeout=120.0,
        )
        resp.raise_for_status()
        data = resp.json()
        vec = data.get("embedding")
        if not isinstance(vec, list):
            raise RuntimeError(
                f"Ollama returned unexpected payload from /api/embeddings: {data!r}"
            )
        if len(vec) != settings.embedding_dim:
            raise RuntimeError(
                f"Ollama returned a {len(vec)}-dim vector but EMBEDDING_DIM is "
                f"{settings.embedding_dim}. Either change the model or update the "
                f"EMBEDDING_DIM env var + the vector(N) type in schema.sql."
            )
        return vec

    if client is not None:
        return await _call(client)
    async with httpx.AsyncClient() as c:
        return await _call(c)


def _embedding_text(chunk: Chunk) -> str:
    """Build the string we hand to bge-m3 for this chunk.

    We include image_captions because they are the only text representation
    of the screenshots (per project policy: no image embeddings, captions
    bridge the modalities). Title + when_to_use + content + image_captions
    is what the user is likely to phrase queries against.
    """
    parts = [chunk.title]
    if chunk.when_to_use:
        parts.append(chunk.when_to_use)
    parts.append(chunk.content)
    if chunk.image_captions:
        parts.append(chunk.image_captions)
    return "\n".join(p for p in parts if p)


async def index_chunks(
    chunks: list[Chunk],
    *,
    doc_title: str,
    manual_version: str,
    source_path: str,
) -> UUID:
    """Embed each chunk, then insert the document + all chunks in one transaction.

    Idempotent: re-running for the same (doc_title, manual_version) marks
    the old chunks `active=false` before inserting the new ones.

    Returns the document id.
    """
    if not chunks:
        raise ValueError("No chunks to index — refusing to ingest an empty document.")

    log.info(
        "Embedding %d chunks for %r v%s via %s",
        len(chunks),
        doc_title,
        manual_version,
        settings.embedding_model,
    )

    # Phase 1: compute embeddings outside the transaction. This is the
    # expensive part (CPU bge-m3 is ~1s per chunk) and we don't want to
    # hold a DB transaction open the whole time.
    embeddings: list[list[float]] = []
    async with httpx.AsyncClient() as http:
        for i, chunk in enumerate(chunks, start=1):
            vec = await embed_text(_embedding_text(chunk), client=http)
            embeddings.append(vec)
            if i % 10 == 0 or i == len(chunks):
                log.info("  embedded %d/%d", i, len(chunks))

    # Phase 2: write everything in a single transaction.
    pool = await get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            doc_id = await queries.insert_document(
                title=doc_title,
                manual_version=manual_version,
                source_path=source_path,
                conn=conn,
            )
            superseded = await queries.mark_document_chunks_inactive(doc_id, conn=conn)
            if superseded > 0:
                log.info("Marked %d previous chunks as inactive (re-ingest)", superseded)

            for chunk, embedding in zip(chunks, embeddings):
                await queries.upsert_chunk(
                    doc_id=doc_id,
                    kind=chunk.kind,
                    section_path=chunk.section_path,
                    title=chunk.title,
                    when_to_use=chunk.when_to_use,
                    content=chunk.content,
                    image_captions=chunk.image_captions,
                    images=chunk.images,
                    ui_labels=chunk.ui_labels,
                    cautions=chunk.cautions,
                    notes=chunk.notes,
                    page_start=chunk.page_start,
                    page_end=chunk.page_end,
                    manual_version=manual_version,
                    embedding=embedding,
                    conn=conn,
                )

    log.info("Indexed %d chunks for document %s", len(chunks), doc_id)
    return doc_id
