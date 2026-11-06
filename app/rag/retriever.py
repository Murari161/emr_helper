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
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID

import httpx

from app.config import settings
from app.db import queries
from app.rag import canonical, reranker

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


@dataclass
class RetrievalResult:
    """Top-k chunks plus how strongly the best candidate matched the query.

    RRF fusion throws the raw lane scores away (it only uses ranks), so the
    chunk list alone can't tell "found the procedure" from "found the least
    irrelevant thing". These two numbers can.
    """
    chunks: list[dict[str, Any]] = field(default_factory=list)
    top_vector: float = 0.0   # best cosine similarity in the vector lane
    top_bm25: float = 0.0     # best ts_rank_cd in the BM25 lane (0 = no lexical hit)
    # Every fused candidate, each with its "vector_score". Used to offer a
    # choice of topics when a question is vague (app/rag/topics.py).
    candidates: list[dict[str, Any]] = field(default_factory=list)

    @property
    def is_strong(self) -> bool:
        """True if the manuals plausibly contain the answer."""
        return (
            self.top_vector >= settings.match_strong_vector
            or self.top_bm25 >= settings.match_strong_bm25
        )


async def retrieve(
    query: str,
    *,
    k: int = 5,
    manual_versions: list[str] | None = None,
) -> list[dict[str, Any]]:
    """Top-k chunks for a query. See retrieve_scored() for the pipeline."""
    result = await retrieve_scored(query, k=k, manual_versions=manual_versions)
    return result.chunks


async def retrieve_scored(
    query: str,
    *,
    k: int = 5,
    manual_versions: list[str] | None = None,
) -> RetrievalResult:
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
        return RetrievalResult()

    import time  # perf profiling (perf/latency-profiling branch)
    _t0 = time.perf_counter()
    _timings: dict[str, float] = {}

    log.info("retrieve(%r, k=%d)", query, k)

    # 1. Embed query
    _t = time.perf_counter()
    qvec = await _embed_query(query)
    _timings["1_embed_query"] = time.perf_counter() - _t

    # 2. Three parallel searches
    _t = time.perf_counter()
    ranked = await queries.hybrid_search(
        query_text=query,
        query_vec=qvec,
        manual_versions=manual_versions,
        k_vector=K_VECTOR,
        k_bm25=K_BM25,
        k_trigram=K_TRIGRAM,
    )
    _timings["2_hybrid_search"] = time.perf_counter() - _t
    log.debug(
        "  candidates: %d vector / %d bm25 / %d trigram",
        len(ranked["vector"]), len(ranked["bm25"]), len(ranked["trigram"]),
    )

    # 3. RRF fusion -> top-K_TO_RERANK
    _t = time.perf_counter()
    fused = _rrf_fuse(ranked)
    _timings["3_rrf_fuse"] = time.perf_counter() - _t
    if not fused:
        log.info("  no candidates after fusion")
        return RetrievalResult()
    top_ids = [doc_id for doc_id, _score in fused[:K_TO_RERANK]]
    # Lane lists are score-descending, so [0] is each lane's best match.
    top_vector = ranked["vector"][0][1] if ranked["vector"] else 0.0
    top_bm25 = ranked["bm25"][0][1] if ranked["bm25"] else 0.0

    # 4. Hydrate the rows
    _t = time.perf_counter()
    chunks = await queries.get_chunks_by_ids(top_ids)
    _timings["4_hydrate_rows"] = time.perf_counter() - _t
    if not chunks:
        return RetrievalResult()

    # 5. Rerank (cross-encoder)
    _t = time.perf_counter()
    chunks = await reranker.rerank(query, chunks)
    _timings["5_rerank"] = time.perf_counter() - _t

    # 5b. A summary that points to a fuller, same-titled procedure in another
    # manual gives way to that full version (see app/rag/canonical.py).
    if settings.prefer_full_procedures:
        _t = time.perf_counter()
        try:
            chunks = await canonical.prefer_full_versions(chunks)
        except Exception:
            log.exception("  could not resolve full versions; keeping the ranked list as is")
        _timings["5b_full_versions"] = time.perf_counter() - _t

    vector_scores = dict(ranked["vector"])
    for c in chunks:
        c["vector_score"] = vector_scores.get(c["id"], 0.0)

    # 6. Top-k
    out = chunks[:k]
    _total = time.perf_counter() - _t0
    _breakdown = "  ".join(f"{name}={secs:.2f}s" for name, secs in _timings.items())
    log.info("  ⏱ retrieve TOTAL=%.2fs  [%s]", _total, _breakdown)
    result = RetrievalResult(chunks=out, top_vector=top_vector, top_bm25=top_bm25, candidates=chunks)
    log.info(
        "  returned %d chunk(s), top-1 title=%r, match vector=%.3f bm25=%.3f strong=%s",
        len(out),
        out[0]["title"] if out else None,
        top_vector,
        top_bm25,
        result.is_strong,
    )
    return result
