"""Talk to the model that writes the answers: the local one or a hosted one.

Two calls need it: the streamed answer (app/rag/generator.py) and a short
label for questions the manuals can't answer (app/rag/classifier.py). Which
model serves them is a setting, GENERATION_PROVIDER, so one codebase serves
the laptop, the facilities and the online trial:

  ollama  the local model on this computer: laptop and facilities
  openai  any OpenAI-compatible chat API: Together AI on the online trial

Search embeddings are not here: they always stay local (app/rag/retriever.py).
The API key is sent only in the request header and never logged.
"""
from __future__ import annotations

import json
import logging
from typing import Any, AsyncIterator

import httpx

from app.config import settings

log = logging.getLogger(__name__)

OLLAMA = "ollama"
OPENAI = "openai"

_STREAM_TIMEOUT = httpx.Timeout(connect=10.0, read=600.0, write=10.0, pool=10.0)
_DONE = object()          # end of an OpenAI-style stream


def provider() -> str:
    """The configured provider, checked."""
    name = (settings.generation_provider or OLLAMA).strip().lower()
    if name not in (OLLAMA, OPENAI):
        raise ValueError(f"GENERATION_PROVIDER must be 'ollama' or 'openai', not {name!r}")
    return name


def ollama_payload(system: str, prompt: str, *, temperature: float, max_tokens: int, stream: bool) -> dict[str, Any]:
    return {
        "model": settings.generation_model,
        "system": system,
        "prompt": prompt,
        "stream": stream,
        "options": {"temperature": temperature, "num_predict": max_tokens},
    }


def openai_payload(system: str, prompt: str, *, temperature: float, max_tokens: int, stream: bool) -> dict[str, Any]:
    return {
        "model": settings.generation_model,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": prompt},
        ],
        "temperature": temperature,
        "max_tokens": max_tokens,
        "stream": stream,
    }


def _openai_url() -> str:
    return settings.generation_api_base.rstrip("/") + "/chat/completions"


def _openai_headers() -> dict[str, str]:
    if not settings.generation_api_key:
        raise RuntimeError("GENERATION_API_KEY is not set, so the hosted model cannot be used")
    return {"Authorization": f"Bearer {settings.generation_api_key}"}


def parse_sse_line(line: str) -> Any:
    """One line of an OpenAI-style stream: the new text ("" if none), _DONE at
    the end, or None for lines to skip (blank lines, keep-alive comments)."""
    line = line.strip()
    if not line.startswith("data:"):
        return None
    data = line[len("data:"):].strip()
    if data == "[DONE]":
        return _DONE
    try:
        event = json.loads(data)
    except json.JSONDecodeError:
        log.warning("Skipping an unreadable line from the answer model's stream")
        return None
    choices = event.get("choices") or []
    if not choices:
        return None
    return (choices[0].get("delta") or {}).get("content") or ""


def _client(timeout: Any) -> httpx.AsyncClient:
    """A new HTTP client. Tests replace this to answer without a network."""
    return httpx.AsyncClient(timeout=timeout)


async def _check(response: httpx.Response) -> None:
    """Raise on an HTTP error, logging the provider's explanation first."""
    if not response.is_error:
        return
    try:
        detail = (await response.aread()).decode("utf-8", "replace")[:300]
    except Exception:
        detail = ""
    log.error("The answer model returned HTTP %d: %s", response.status_code, detail)
    response.raise_for_status()


async def stream_completion(
    system: str, prompt: str, *, temperature: float, max_tokens: int
) -> AsyncIterator[str]:
    """Yield the answer text as the model writes it."""
    if provider() == OPENAI:
        payload = openai_payload(system, prompt, temperature=temperature, max_tokens=max_tokens, stream=True)
        async with _client(_STREAM_TIMEOUT) as client:
            async with client.stream("POST", _openai_url(), json=payload, headers=_openai_headers()) as response:
                await _check(response)
                async for line in response.aiter_lines():
                    token = parse_sse_line(line)
                    if token is _DONE:
                        return
                    if token:
                        yield token
        return

    payload = ollama_payload(system, prompt, temperature=temperature, max_tokens=max_tokens, stream=True)
    async with _client(_STREAM_TIMEOUT) as client:
        async with client.stream("POST", f"{settings.ollama_base_url}/api/generate", json=payload) as response:
            await _check(response)
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


async def complete(
    system: str, prompt: str, *, temperature: float, max_tokens: int, timeout: float = 120.0
) -> str:
    """The model's whole reply, for short calls such as the classifier."""
    if provider() == OPENAI:
        payload = openai_payload(system, prompt, temperature=temperature, max_tokens=max_tokens, stream=False)
        async with _client(timeout) as client:
            response = await client.post(_openai_url(), json=payload, headers=_openai_headers())
            await _check(response)
            data = response.json()
        choices = data.get("choices") or [{}]
        return (choices[0].get("message") or {}).get("content") or ""

    payload = ollama_payload(system, prompt, temperature=temperature, max_tokens=max_tokens, stream=False)
    async with _client(timeout) as client:
        response = await client.post(f"{settings.ollama_base_url}/api/generate", json=payload)
        await _check(response)
        return response.json().get("response", "")
