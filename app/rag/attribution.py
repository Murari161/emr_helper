"""Which retrieved chunk did the answer actually come from?

The model is given five chunks and told to answer from one. Screenshots, the
citation and the cross-reference buttons must all come from THAT chunk.

The original heuristic looked for chunk titles inside the answer. It breaks in
two common cases seen in testing:
  - Twin titles. "Send a patient for an imaging test" exists in the Doctor
    manual and (as "…(direct imaging test)") in Patient Management. The answer
    reproduced the Patient Management steps but opened with the Doctor title,
    so it got the Doctor manual's screenshots and citation.
  - A procedure merely mentioned ("for a returning patient, follow *Queue a
    returning patient*") was treated as a source, and its screenshots were
    appended to an answer that is not about queueing.

A faithful answer reproduces its chunk's steps, so word overlap with the chunk
body identifies the source far more reliably than the title does.
"""
from __future__ import annotations

import re
from typing import Any

# Minimum share of the answer's distinctive words that must come from a chunk
# before we call it the source. Faithful answers score 0.7-0.95.
MIN_COVERAGE = 0.5

_STOPWORDS = frozenset(
    "a an the to of in on for and or is are be this that it its at by with from as will "
    "you your then now can has have into all also which their there here follow these steps".split()
)


def tokens(text: str) -> set[str]:
    """Distinctive lower-case words of a text."""
    return {w for w in re.findall(r"[a-z0-9+]+", text.lower()) if w not in _STOPWORDS and len(w) > 1}


def coverage(answer: str, chunk: dict[str, Any]) -> float:
    """Share of the answer's distinctive words that appear in this chunk."""
    said = tokens(answer)
    if not said:
        return 0.0
    source = tokens(" ".join(
        str(chunk.get(field) or "") for field in ("title", "when_to_use", "content", "cautions", "notes")
    ))
    return len(said & source) / len(said)


def source_chunk(answer: str, chunks: list[dict[str, Any]]) -> dict[str, Any] | None:
    """The chunk the answer was written from, or None if no chunk clearly is.

    Ties go to the higher-ranked chunk (earlier in `chunks`).
    """
    best, best_cov = None, 0.0
    for c in chunks:
        cov = coverage(answer, c)
        if cov > best_cov:
            best, best_cov = c, cov
    return best if best_cov >= MIN_COVERAGE else None
