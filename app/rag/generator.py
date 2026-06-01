"""Stream answers from Ollama, given a user query and retrieved context chunks.

The function is one async generator: callers `async for token in stream_answer(...)`.
That makes it trivial to pipe into Chainlit's `cl.Message.stream_token(...)`.

Streaming is non-negotiable here. With llama3.2:3b on CPU, generating 200
tokens (a typical procedure answer) takes ~6-15 seconds end-to-end. Without
streaming, the user stares at nothing for that whole period; with streaming
they see the first word in ~500 ms and the answer flows in front of them.
The perceived latency drops by an order of magnitude.

Ollama's /api/generate with stream=True returns one JSON-per-line, each
with a 'response' field carrying the next token chunk and a 'done' field
that's true on the final line. We forward 'response' strings as-is.
"""
from __future__ import annotations

import json
import logging
from typing import Any, AsyncIterator

import httpx

from app.config import settings
from app.rag.prompts import ANSWER_SYSTEM_PROMPT, build_user_prompt

log = logging.getLogger(__name__)


async def stream_answer(
    query: str,
    chunks: list[dict[str, Any]],
    *,
    temperature: float = 0.2,
) -> AsyncIterator[str]:
    """Yield answer tokens as they come from Ollama.

    Low temperature (0.2) keeps the model close to the source text — we
    want it to reproduce the manual's steps faithfully, not paraphrase
    creatively. Raising this hurts factual accuracy on this corpus.

    If there are no chunks (retrieval came back empty), we yield a fixed
    "I don't have that in the manuals" message without calling the LLM.
    """
    if not chunks:
        yield (
            "I don't have that in the manuals.\n\n"
            "If this is a clinical question, please consult a clinician or "
            "your clinical reference. If it's a system question, the answer "
            "may be in a manual that hasn't been ingested yet."
        )
        return

    user_prompt = build_user_prompt(query, chunks)
    payload = {
        "model": settings.generation_model,
        "system": ANSWER_SYSTEM_PROMPT,
        "prompt": user_prompt,
        "stream": True,
        "options": {
            "temperature": temperature,
            # Cap output length — long answers tend to drift off-topic for
            # a 3B model and we want responsive UX.
            "num_predict": 800,
        },
    }

    log.info(
        "stream_answer: model=%s, %d chunks, query=%r",
        settings.generation_model,
        len(chunks),
        query[:80],
    )

    timeout = httpx.Timeout(connect=10.0, read=600.0, write=10.0, pool=10.0)
    async with httpx.AsyncClient(timeout=timeout) as client:
        async with client.stream(
            "POST",
            f"{settings.ollama_base_url}/api/generate",
            json=payload,
        ) as response:
            response.raise_for_status()
            async for line in response.aiter_lines():
                if not line:
                    continue
                try:
                    data = json.loads(line)
                except json.JSONDecodeError:
                    log.warning("Skipping non-JSON line from Ollama stream: %r", line)
                    continue
                token = data.get("response")
                if token:
                    yield token
                if data.get("done"):
                    return
