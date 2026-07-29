"""Cross-encoder reranker — re-scores (query, chunk) pairs with bge-reranker-v2-m3.

Why we run this in-process (sentence-transformers + torch CPU) rather than via Ollama:
Ollama's `/api/embeddings` is bi-encoder-only — it returns one vector per text, not
a relevance score per (query, doc) pair. Cross-encoders take both inputs together
and output a single relevance scalar; that's the right tool here and Ollama
doesn't expose one. sentence-transformers' CrossEncoder wraps the model cleanly.

Why this matters: empirically, the RRF-only retriever ranks the literal target
procedure 4th or 5th for ambiguous short queries ("what does +New do",
"how do I register a new patient") because token-density-based BM25 prefers
shorter overview/index chunks. The cross-encoder reads the query and the full
chunk content semantically and ranks the answer chunk #1. On the 12-query
golden set this is the difference between ~83% and 100% top-1 accuracy.

Performance notes (CPU):
  - Model load (~600 MB download, then ~3s init on subsequent restarts): one-time per process.
  - Scoring 20 (query, doc) pairs: ~1-2 s on a modern CPU. Acceptable for one
    user message; would need batching tweaks for high-concurrency workloads.

The model loads lazily on first call so test runs that don't use reranking
don't pay the import cost.
"""
from __future__ import annotations

import logging
from typing import Any

from app.config import settings

log = logging.getLogger(__name__)

# Lazy singleton — initialized on first rerank() call.
_model: Any | None = None
_MODEL_NAME = "BAAI/bge-reranker-v2-m3"
# Max chars we feed to the cross-encoder per chunk. Truncates very long chunks
# to keep inference time bounded. bge-reranker-v2-m3 itself handles up to 8k tokens.
_MAX_CHUNK_CHARS = 4000


def _get_model() -> Any:
    global _model
    if _model is None:
        # Import locally so test runs that don't hit reranker don't pay the
        # cost of pulling in torch.
        from sentence_transformers import CrossEncoder
        log.info("Loading cross-encoder %s (one-time)…", _MODEL_NAME)
        _model = CrossEncoder(_MODEL_NAME, max_length=512)
        log.info("Cross-encoder ready.")
    return _model


def warmup() -> None:
    """Eagerly load the cross-encoder so the first user query doesn't pay
    the model-load latency. Safe to call multiple times (singleton check).

    Called from app/main.py at process startup. If the model is being
    downloaded for the first time (cache empty) this can take minutes; the
    docker-compose `hf_cache` named volume keeps the download persistent so
    subsequent container starts are instant.
    """
    if not settings.reranker_enabled:
        log.info("Reranker DISABLED (RERANKER_ENABLED=false) — skipping model load.")
        return
    try:
        _get_model()
    except Exception:
        log.exception("Reranker warmup failed; will retry lazily on first query")


def _chunk_text_for_scoring(chunk: dict[str, Any]) -> str:
    """Build the text we hand to the cross-encoder as the 'document' side.

    We include title + when_to_use + content + image_captions because all four
    carry signal. Truncated to keep inference time predictable.
    """
    parts = [chunk.get("title") or ""]
    if chunk.get("when_to_use"):
        parts.append(chunk["when_to_use"])
    if chunk.get("content"):
        parts.append(chunk["content"])
    if chunk.get("image_captions"):
        parts.append(chunk["image_captions"])
    text = "\n".join(p for p in parts if p)
    return text[:_MAX_CHUNK_CHARS]


async def rerank(query: str, chunks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Reorder `chunks` for `query` using the cross-encoder.

    On any failure (model load error, scoring error) we log and fall back
    to the input order — retrieval should degrade gracefully rather than
    break the user's session.
    """
    if not chunks:
        return chunks

    if not settings.reranker_enabled:
        # Skip the cross-encoder entirely — keep the RRF/fusion order.
        return chunks

    try:
        model = _get_model()
        pairs = [(query, _chunk_text_for_scoring(c)) for c in chunks]
        # predict() is sync (PyTorch CPU). asyncio.to_thread would offload to
        # a worker thread; for a few-second call inside Chainlit's per-message
        # handler it's fine to block briefly. Phase 5 can move this to a
        # threadpool if needed.
        scores = model.predict(pairs)
        ranked = sorted(zip(chunks, scores), key=lambda x: float(x[1]), reverse=True)
        log.debug(
            "Rerank: top-3 scores after = %s",
            [(c["title"], float(s)) for c, s in ranked[:3]],
        )
        return [c for c, _ in ranked]
    except Exception:
        log.exception("rerank() failed; falling back to RRF order")
        return chunks
