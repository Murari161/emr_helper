"""Tests for the answer-model client (app/rag/llm.py) and its two callers.

No network: every request is answered by a fake server (httpx.MockTransport).
    docker compose exec app pytest tests/test_llm.py -v

What each test catches:

  test_hosted_model_streams_the_answer
      The online trial's path (Together AI): the right address, the key in the
      header, system + user messages, and the streamed pieces joined in order.

  test_local_model_is_unchanged
      Laptops and facilities still call Ollama exactly as before.

  test_key_never_reaches_the_logs
      A rejected key is logged as an error without the key itself.

  test_missing_key_fails_before_any_request
      A server set to the hosted model but without a key says so plainly.

  test_classifier_falls_back_safely
      If the hosted model fails, a question the manuals can't answer still
      gets the Focal Person reply rather than an error.
"""
from __future__ import annotations

import asyncio
import json
import logging

import httpx
import pytest

from app.config import settings
from app.rag import classifier, generator, llm

KEY = "test-key-123"


def use_fake_server(monkeypatch, handler) -> list[httpx.Request]:
    seen: list[httpx.Request] = []

    def record(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return handler(request)

    monkeypatch.setattr(llm, "_client", lambda timeout: httpx.AsyncClient(transport=httpx.MockTransport(record)))
    return seen


def hosted(monkeypatch, key: str = KEY) -> None:
    monkeypatch.setattr(settings, "generation_provider", "openai")
    monkeypatch.setattr(settings, "generation_api_base", "https://api.together.xyz/v1/")
    monkeypatch.setattr(settings, "generation_api_key", key)
    monkeypatch.setattr(settings, "generation_model", "meta-llama/Llama-3.2-3B-Instruct-Turbo")


def sse(*pieces: str) -> bytes:
    events = [": keep-alive", json.dumps({"choices": [{"delta": {"role": "assistant"}}]})]
    events += [json.dumps({"choices": [{"delta": {"content": p}}]}) for p in pieces]
    lines = [e if e.startswith(":") else f"data: {e}" for e in events] + ["data: [DONE]"]
    return ("\n\n".join(lines) + "\n\n").encode()


async def collect(stream) -> list[str]:
    return [token async for token in stream]


def test_hosted_model_streams_the_answer(monkeypatch) -> None:
    hosted(monkeypatch)
    seen = use_fake_server(monkeypatch, lambda r: httpx.Response(200, content=sse("1. Click ", "**Save**.")))
    tokens = asyncio.run(collect(llm.stream_completion("SYSTEM", "QUESTION", temperature=0.2, max_tokens=800)))
    assert "".join(tokens) == "1. Click **Save**."
    request = seen[0]
    assert str(request.url) == "https://api.together.xyz/v1/chat/completions"
    assert request.headers["authorization"] == f"Bearer {KEY}"
    body = json.loads(request.content)
    assert body["model"] == "meta-llama/Llama-3.2-3B-Instruct-Turbo"
    assert body["messages"] == [{"role": "system", "content": "SYSTEM"}, {"role": "user", "content": "QUESTION"}]
    assert body["stream"] is True and body["max_tokens"] == 800 and body["temperature"] == 0.2


def test_hosted_model_short_reply(monkeypatch) -> None:
    hosted(monkeypatch)
    use_fake_server(monkeypatch, lambda r: httpx.Response(200, json={"choices": [{"message": {"content": "CLINICAL"}}]}))
    assert asyncio.run(llm.complete("S", "P", temperature=0.0, max_tokens=6)) == "CLINICAL"


def test_local_model_is_unchanged(monkeypatch) -> None:
    monkeypatch.setattr(settings, "generation_provider", "ollama")
    lines = [json.dumps({"response": "Hel"}), "", "not json", json.dumps({"response": "lo", "done": True})]
    seen = use_fake_server(monkeypatch, lambda r: httpx.Response(200, content="\n".join(lines).encode()))
    tokens = asyncio.run(collect(llm.stream_completion("SYSTEM", "QUESTION", temperature=0.2, max_tokens=800)))
    assert "".join(tokens) == "Hello"
    request = seen[0]
    assert str(request.url).endswith("/api/generate") and "authorization" not in request.headers
    body = json.loads(request.content)
    assert body["system"] == "SYSTEM" and body["prompt"] == "QUESTION"
    assert body["options"] == {"temperature": 0.2, "num_predict": 800}


def test_key_never_reaches_the_logs(monkeypatch, caplog) -> None:
    hosted(monkeypatch)
    use_fake_server(monkeypatch, lambda r: httpx.Response(401, json={"error": "invalid api key"}))
    with caplog.at_level(logging.DEBUG), pytest.raises(httpx.HTTPStatusError):
        asyncio.run(collect(llm.stream_completion("S", "P", temperature=0.2, max_tokens=10)))
    assert "401" in caplog.text and "invalid api key" in caplog.text
    assert KEY not in caplog.text


def test_missing_key_fails_before_any_request(monkeypatch) -> None:
    hosted(monkeypatch, key="")
    seen = use_fake_server(monkeypatch, lambda r: httpx.Response(200))
    with pytest.raises(RuntimeError, match="GENERATION_API_KEY"):
        asyncio.run(llm.complete("S", "P", temperature=0.0, max_tokens=6))
    assert seen == []


def test_unknown_provider_is_refused(monkeypatch) -> None:
    monkeypatch.setattr(settings, "generation_provider", "groq-ish")
    with pytest.raises(ValueError):
        llm.provider()


@pytest.mark.parametrize(
    "line,expected",
    [("", None), (": keep-alive", None), ("event: ping", None), ("data: not json", None),
     ('data: {"choices": []}', None), ('data: {"choices": [{"delta": {"role": "assistant"}}]}', "")],
)
def test_stream_lines(line, expected) -> None:
    assert llm.parse_sse_line(line) == expected


def test_stream_end() -> None:
    assert llm.parse_sse_line("data: [DONE]") is llm._DONE


def test_answer_uses_the_configured_model(monkeypatch) -> None:
    hosted(monkeypatch)
    use_fake_server(monkeypatch, lambda r: httpx.Response(200, content=sse("Steps")))
    chunk = {"id": 1, "title": "Register a patient", "content": "Steps:\nClick.", "section_path": "S > Register a patient"}
    assert asyncio.run(collect(generator.stream_answer("How do I register?", [chunk]))) == ["Steps"]


def test_classifier_uses_the_hosted_model(monkeypatch) -> None:
    hosted(monkeypatch)
    use_fake_server(monkeypatch, lambda r: httpx.Response(200, json={"choices": [{"message": {"content": "Patient_data."}}]}))
    assert asyncio.run(classifier.classify_question("How many patients today?")) == "PATIENT_DATA"


def test_classifier_falls_back_safely(monkeypatch) -> None:
    hosted(monkeypatch)
    use_fake_server(monkeypatch, lambda r: httpx.Response(503, text="overloaded"))
    assert asyncio.run(classifier.classify_question("Which drug for malaria?")) == classifier.SAFE_LABEL
