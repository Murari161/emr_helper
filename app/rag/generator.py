"""Stream answers, given a user query and retrieved context chunks.

The function is one async generator: callers `async for token in stream_answer(...)`.
That makes it trivial to pipe into Chainlit's `cl.Message.stream_token(...)`.

Streaming is non-negotiable here. With llama3.2:3b on CPU, a typical procedure
answer takes many seconds end-to-end. Without streaming, the user stares at
nothing for that whole period; with streaming they see the answer flow in
front of them, so the perceived wait drops sharply.

Which model writes the answer (the local one or a hosted one) is decided in
app/rag/llm.py by GENERATION_PROVIDER.
"""
from __future__ import annotations

import logging
from typing import Any, AsyncIterator

from app.config import settings
from app.rag import llm
from app.rag.fallback import NOT_COVERED_REPLY
from app.rag.prompts import ANSWER_SYSTEM_PROMPT, build_user_prompt

log = logging.getLogger(__name__)

# Long answers tend to drift off-topic for a 3B model, and we want a
# responsive chat.
MAX_ANSWER_TOKENS = 800


async def stream_answer(
    query: str,
    chunks: list[dict[str, Any]],
    *,
    temperature: float = 0.2,
) -> AsyncIterator[str]:
    """Yield answer tokens as the model writes them.

    Low temperature (0.2) keeps the model close to the source text — we
    want it to reproduce the manual's steps faithfully, not paraphrase
    creatively. Raising this hurts factual accuracy on this corpus.

    If there are no chunks (retrieval came back empty), we yield the fixed
    "not in the manuals — contact your Focal Person" message without calling
    the model.
    """
    if not chunks:
        yield NOT_COVERED_REPLY
        return

    log.info(
        "stream_answer: %s model=%s, %d chunks, query=%r",
        llm.provider(),
        settings.generation_model,
        len(chunks),
        query[:80],
    )
    async for token in llm.stream_completion(
        ANSWER_SYSTEM_PROMPT,
        build_user_prompt(query, chunks),
        temperature=temperature,
        max_tokens=MAX_ANSWER_TOKENS,
    ):
        yield token
