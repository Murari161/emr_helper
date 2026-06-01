"""Acceptance tests for the hybrid retrieval pipeline.

These run against the LIVE database — the chunks ingested by Phase 3 must
be present and active. Run with:

    docker compose exec app pytest tests/test_retrieval.py -v

For each golden (query, expected_procedure_title) pair we run the full
pipeline (embed -> hybrid search -> RRF -> rerank -> top-k) and assert
the top-1 result's title contains the expected procedure name (case-
insensitive substring match).

If a query fails: do NOT just bump `k` or tune the SQL until it passes.
First diagnose: is the chunking right? Are the captions strong? Is the
fusion math correct? Tuning around bad data papers over real issues.

The 12 queries are sourced from the project's CLAUDE_CODE_PROMPT.md.
"""
from __future__ import annotations

import pytest

from app.rag.retriever import retrieve

# Single shared event loop for the module — see tests/test_db.py for the
# rationale. Without this the asyncpg pool gets recreated per test and
# trips on the closed event loop from the previous test.
pytestmark = pytest.mark.asyncio(loop_scope="module")


# (query, expected_procedure_title_substring)
GOLDEN: list[tuple[str, str]] = [
    ("how do I register a new patient",                            "Register a patient"),
    ("where is the save button on the registration form",          "Register a patient"),
    ("how do I add someone to the queue",                          "Queue a patient for a doctor"),
    ("a returning patient already has a number, how do I queue them", "Queue a returning patient"),
    ("how do I close a clinic session",                            "Close a clinic session"),
    ("a patient went home and I forgot to close their visit",      "Close a clinic session"),
    ("what does +New do",                                          "Register a patient"),
    ("how do I delete a wrong appointment",                        "Delete an appointment"),
    ("how do I merge duplicate facilities",                        "Merge referring facilities"),
    ("what is a debtor tracking account",                          "Debtor Tracking Account"),  # glossary hit accepted
    ("how do I send a patient for an X-ray",                       "Send a patient for imaging"),
    ("how do I activate a waiver for a prisoner",                  "Activate a waiver"),
]


@pytest.mark.parametrize("query,expected", GOLDEN, ids=[g[0][:40] for g in GOLDEN])
async def test_top1_matches_expected(query: str, expected: str) -> None:
    """For each golden query, the top-1 chunk's title must contain the
    expected procedure name (case-insensitive substring match)."""
    chunks = await retrieve(query, k=5)
    assert chunks, f"No results for query: {query!r}"
    top1 = chunks[0]
    assert expected.lower() in top1["title"].lower(), (
        f"Top-1 mismatch.\n"
        f"  query:    {query!r}\n"
        f"  expected: contains {expected!r}\n"
        f"  got:      {top1['title']!r} (kind={top1['kind']})\n"
        f"  top-5 titles: {[c['title'] for c in chunks]}"
    )
