"""Vague questions: offer a choice of topics instead of guessing.

Stakeholder request (Oct 2026): when a question is too vague to answer ("billing",
"how do I approve"), list the closest topics and ask which one was meant, with a
tip on how to phrase the question.

What counts as vague, measured on 34 vague and 61 clear or scenario questions:
  - The number of meaningful words is the main signal. Long scenario questions
    can have tiny score gaps between topics and are still perfectly answerable.
  - One meaningful word is vague ("queue", "how do I send").
  - Two are vague only if several topics score almost equally ("patient
    records"); "How do I cancel an appointment?" has one clear winner.
Never vague: definition questions ("What is a replenishment level?"), a question
that names a topic ("register a patient"), a literal button label ("+New"), and
words the manuals don't contain ("weather"), which go on to the normal off-topic
handling. A vague question with no related topic gets a phrasing tip, not a list
of unrelated procedures.

Pure functions only (no Chainlit, no database), so they are unit-testable.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from app.rag import classifier, fallback

# Two meaningful words are vague when the best two topics are closer than this.
MIN_GAP = 0.03
# With no keyword hit in the manuals, a question needs at least this similarity
# before it is treated as being about eAFYA at all.
MIN_VECTOR = 0.55
MAX_OPTIONS = 4

# Words that carry no information about WHICH task is meant.
_STOPWORDS = frozenset("""
    a about am an and any are as at be but by can could did do does for from get go going
    had has have how i in into is it its me my need not of on or our please should so t
    that the their them then there these this to us want was we were what when where
    which who why will with would you your
    button buttons eafya emr help issue issues module modules page problem problems
    screen screens something system tab thing things use using work working works
""".split())

_DEFINITION_RE = re.compile(
    r"^\s*(what\s+(is|are|does|do)\b|what's\b|define\b|meaning\s+of\b|explain\b)", re.IGNORECASE
)

CLARIFY_REPLY = (
    "That could mean a few things in eAFYA. Which of these did you mean?\n\n"
    "*Tip: you can also ask directly by saying what you want to do, for example: "
    "How do I approve a leave request?*"
)
CLARIFY_NO_TOPICS_REPLY = (
    "Could you tell me a little more? Say what you are trying to do and on which "
    "screen or module, for example: *How do I approve a leave request?*\n\n"
    f"If something in eAFYA is not working, your facility's **{fallback.FOCAL_PERSON}** can help."
)
RELATED_TOPICS_INTRO = "These topics in the manuals may be related:"


@dataclass
class Clarification:
    reply: str
    options: list[dict[str, Any]] = field(default_factory=list)


def meaningful_words(text: str) -> set[str]:
    """The words of a question or title that say which task is meant."""
    text = re.sub(r"\(.*?\)", " ", text.lower())
    return {w for w in re.findall(r"[a-z0-9+]+", text) if w not in _STOPWORDS}


def _title_key(chunk: dict[str, Any]) -> str:
    return re.sub(r"\s*\(.*?\)\s*$", "", (chunk.get("title") or "").lower()).strip()


def _ranked_topics(candidates: list[dict[str, Any]]) -> list[tuple[float, dict[str, Any] | None]]:
    """Distinct topics, best first: (score, procedure chunk to open, if any).

    The same title can appear as a procedure and as Quick-Index entries, or in
    several manuals. A topic scores as its best-matching chunk, and opens the
    best-matching procedure of that title.
    """
    score: dict[str, float] = {}
    proc: dict[str, dict[str, Any]] = {}
    for c in candidates:
        if c.get("kind") not in ("procedure", "index_entry"):
            continue
        key, s = _title_key(c), float(c.get("vector_score") or 0.0)
        score[key] = max(score.get(key, 0.0), s)
        if c.get("kind") == "procedure" and not fallback.is_stub(c):
            if key not in proc or s > float(proc[key].get("vector_score") or 0.0):
                proc[key] = c
    return [(s, proc.get(key)) for key, s in sorted(score.items(), key=lambda kv: -kv[1])]


def topic_options(
    candidates: list[dict[str, Any]],
    *,
    limit: int = MAX_OPTIONS,
    min_score: float = 0.0,
) -> list[dict[str, Any]]:
    """The procedures to offer as choices, best first. Placeholders are left out."""
    ranked = _ranked_topics(candidates)
    return [c for s, c in ranked if c is not None and s >= min_score][:limit]


def _names_topic(chunk: dict[str, Any], words: set[str]) -> bool:
    """True if the question contains every meaningful word of this chunk's title
    ("How do I use the stores tab in the pharmacy?" names "Use the stores tab")."""
    title_words = meaningful_words(chunk.get("title") or "")
    return bool(title_words) and title_words <= words


def _is_related(chunk: dict[str, Any], words: set[str]) -> bool:
    """True if the topic's title or manual shares a word with the question.

    Compared on the first four letters, so "drugs" finds "Add a drug" and
    "pharmacy" finds the Pharmacy manual. Search alone can return unrelated
    procedures for a word the manuals barely use ("how do I search").
    """
    text = f"{chunk.get('title') or ''} {chunk.get('manual') or ''}".lower()
    return any(w[:4] in text for w in words)


def _is_vague(query: str, result: Any) -> bool:
    words = meaningful_words(query)
    if len(words) > 2 or _DEFINITION_RE.match(query) or classifier._ABOUT_RE.search(query):
        return False
    if not words:
        return True
    if any(w.startswith("+") for w in words):      # a literal button label
        return False
    if result.top_bm25 <= 0 and result.top_vector < MIN_VECTOR:
        return False                               # not about anything in the manuals
    if any(_names_topic(c, words) for c in result.candidates):
        return False                               # the question names a topic
    if len(words) == 1:
        return True
    ranked = _ranked_topics(result.candidates)
    return len(ranked) >= 2 and ranked[0][0] - ranked[1][0] < MIN_GAP


def clarification(query: str, result: Any) -> Clarification | None:
    """What to ask back if the question is too vague to answer, else None."""
    if not _is_vague(query, result):
        return None
    words = meaningful_words(query)
    if not words:
        return Clarification(reply=CLARIFY_NO_TOPICS_REPLY)
    options = [c for c in topic_options(result.candidates, limit=20) if _is_related(c, words)]
    if not options:
        return Clarification(reply=CLARIFY_NO_TOPICS_REPLY)
    if len(options) == 1:
        return None                                # only one topic fits: answer normally
    return Clarification(reply=CLARIFY_REPLY, options=options[:MAX_OPTIONS])
