"""Hybrid retrieval: vector + BM25 + trigram, fused via Reciprocal Rank Fusion.

The contract is one function:

    retrieve(query: str, k: int = 5) -> list[dict]

It returns up to `k` chunk rows (as dicts), ordered by relevance, each with
the same columns as the chunks table. The fusion step combines three
ranking signals that complement each other:

    Vector   — semantic similarity via bge-m3 embeddings.
               Catches paraphrase queries ('how to register a new patient'
               vs. the procedure title 'Register a patient').

    BM25     — lexical overlap on the title + when_to_use + content +
               image_captions full-text index. Catches exact-word queries
               and proper nouns.

    Trigram  — fuzzy matching on ui_labels (the bold UI element names).
               Catches queries that mention an unusual token like '+New'
               or 'Save to Queue' that BM25 may tokenize away.

After fusion, the top-20 fused candidates are passed to a reranker
(currently a pass-through; see app/rag/reranker.py for the rationale).
The final top-k (default 5) are returned.

Why RRF instead of weighted-sum fusion: RRF doesn't require score
normalisation across the three lanes, which is otherwise nasty because
cosine similarity, ts_rank_cd, and trigram similarity have wildly
different ranges. RRF only cares about *ranks*, which makes it robust
to those scale mismatches. Constant k=60 is the original Cormack et al.
recommendation and works well in practice.
"""
from __future__ import annotations

import logging
from typing import Any
from uuid import UUID

import httpx

from app.config import settings
from app.db import queries
from app.rag import reranker

log = logging.getLogger(__name__)

# RRF tunable. 60 is the canonical default from the Cormack/Clarke/Buettcher
# paper. Higher k flattens differences between ranks (more democratic);
# lower k amplifies top-rank dominance.
RRF_K = 60

# How many candidates to retrieve from each lane before fusion.
K_VECTOR = 50
K_BM25 = 50
K_TRIGRAM = 30

# How many fused candidates the reranker sees.
K_TO_RERANK = 20


async def _embed_query(query: str) -> list[float]:
    """Get the bge-m3 embedding for a query string."""
    async with httpx.AsyncClient() as http:
        resp = await http.post(
            f"{settings.ollama_base_url}/api/embeddings",
            json={"model": settings.embedding_model, "prompt": query},
            timeout=60.0,
        )
        resp.raise_for_status()
        data = resp.json()
        vec = data.get("embedding")
        if not isinstance(vec, list) or len(vec) != settings.embedding_dim:
            raise RuntimeError(f"Bad embedding payload from Ollama: {data!r}")
        return vec


def _rrf_fuse(
    ranked_lists: dict[str, list[tuple[UUID, float]]],
    *,
    k: int = RRF_K,
) -> list[tuple[UUID, float]]:
    """Fuse multiple ranked lists into one ranking via Reciprocal Rank Fusion.

    For each document d and each ranked list L:
        RRF_score(d) += 1 / (k + rank_L(d))
    where rank starts at 1. Documents not in a list contribute 0 from it.
    The output is sorted by RRF score descending.

    The original scores in each list are unused — RRF cares only about
    rank position. This makes it robust across heterogeneous score scales.
    """
    rrf_scores: dict[UUID, float] = {}
    for _lane_name, ranked in ranked_lists.items():
        for rank, (doc_id, _score) in enumerate(ranked, start=1):
            rrf_scores[doc_id] = rrf_scores.get(doc_id, 0.0) + 1.0 / (k + rank)

    fused = sorted(rrf_scores.items(), key=lambda x: x[1], reverse=True)
    return fused


async def retrieve(
    query: str,
    *,
    k: int = 5,
    manual_versions: list[str] | None = None,
) -> list[dict[str, Any]]:
    """Run the full hybrid pipeline for a single user query.

    Steps:
      1. Embed the query (Ollama bge-m3).
      2. Run three parallel searches against pgvector.
      3. Fuse via RRF -> top-K_TO_RERANK candidates.
      4. Fetch full rows for those candidates.
      5. Rerank (currently pass-through).
      6. Return top-k.

    manual_versions=None means "search across all loaded versions" — fine
    for v1 where we only have one version. Phase 6+ will use this for
    A/B testing across manual revisions.
    """
    if not query.strip():
        return []

    log.info("retrieve(%r, k=%d)", query, k)

    # 1. Embed query
    qvec = await _embed_query(query)

    # 2. Three parallel searches
    ranked = await queries.hybrid_search(
        query_text=query,
        query_vec=qvec,
        manual_versions=manual_versions,
        k_vector=K_VECTOR,
        k_bm25=K_BM25,
        k_trigram=K_TRIGRAM,
    )
    log.debug(
        "  candidates: %d vector / %d bm25 / %d trigram",
        len(ranked["vector"]), len(ranked["bm25"]), len(ranked["trigram"]),
    )

    # 3. RRF fusion -> top-K_TO_RERANK
    fused = _rrf_fuse(ranked)
    if not fused:
        log.info("  no candidates after fusion")
        return []
    top_ids = [doc_id for doc_id, _score in fused[:K_TO_RERANK]]

    # 4. Hydrate the rows
    chunks = await queries.get_chunks_by_ids(top_ids)
    if not chunks:
        return []

    # 5. Rerank (pass-through in v1)
    chunks = await reranker.rerank(query, chunks)

    # 6. Top-k
    out = chunks[:k]
    log.info(
        "  returned %d chunk(s), top-1 title=%r",
        len(out),
        out[0]["title"] if out else None,
    )
    return out
