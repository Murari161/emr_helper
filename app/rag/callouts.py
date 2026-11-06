"""Keep the answer's Caution and Note lines true to the manual.

The system prompt tells the model to include a Caution or Note only if the
chunk has it, and never to relabel one as the other. The 3B model does not
reliably obey. Seen in stakeholder testing:
  - "Add a bill allocation" has no caution and no note, yet the answer ended
    with "⚠️ Caution: Ensure you have entered accurate billing information…".
  - "Approve a leave request" has a Note about the approval mandate; the
    answer showed it as a Caution.
An invented warning in a health system looks official and is not in the
manual, so this is checked in code after every answer:
  - a Caution line must match the chunk's cautions; if it matches its notes
    or body instead it is shown as a Note; otherwise it is removed;
  - a Note line must match the chunk's notes or body; if it matches a caution
    it is shown as a Caution; otherwise it is removed;
  - any caution or note the procedure has but the answer left out is added,
    in the manual's words (add_missing_callouts).

Stakeholders also asked that users be sent only to the eAFYA Focal Person,
but some manual notes say "contact the System Administrator". Answers say
"your facility's eAFYA Focal Person" instead; the manuals are unchanged.
"""
from __future__ import annotations

import logging
import re
from typing import Any

from app.rag.attribution import tokens
from app.rag.fallback import FOCAL_PERSON

log = logging.getLogger(__name__)

# Share of a line's meaningful words that must appear in the manual text for
# the line to count as coming from it. Paraphrases score 0.7-1.0; invented
# advice that merely reuses a word or two of the procedure scores far lower.
MIN_MATCH = 0.6

_CALLOUT_RE = re.compile(
    r"^(?P<lead>\s*(?:[-*•]\s+)?)\(?\s*(?:⚠️|⚠)?\s*\**\s*"
    r"(?P<label>caution|warning|important|note)\s*\**\s*:\s*\**\s*(?P<body>.*?)\)?\s*$",
    re.IGNORECASE,
)
_CAUTION_LABELS = {"caution", "warning", "important"}

# Also takes a "(configured in …)" right after it: in the manuals that bracket
# describes the approval mandate, and would read as if it described the Focal
# Person. The "Open: User and Facility Management" button still points there.
_SYSADMIN_RE = re.compile(
    r"(?:\b(?:your|the)\s+)?\**system\s+admin(?:istrator)?s?\b\**(?:\s*\(configured in [^)]*\))?",
    re.IGNORECASE,
)
FOCAL_PERSON_PHRASE = f"your facility's **{FOCAL_PERSON}**"


def _match(line: str, source: str) -> float:
    said = tokens(line)
    if not said:
        return 0.0
    return len(said & tokens(source)) / len(said)


def callout_md(kind: str, text: str, indent: str = "") -> list[str]:
    """A Caution or Note as its own Markdown blockquote.

    A blockquote sets it apart from the steps even unstyled; public/script.js
    then tags it so public/style.css colours it like the cleaned manuals
    (amber Note, red Caution). The blank lines keep the next paragraph from
    being pulled into the quote.
    """
    label = "⚠️ **Caution:**" if kind == "caution" else "**Note:**"
    return ["", f"{indent}> {label} {text}", ""]


def check_callouts(answer: str, chunks_used: list[dict[str, Any]]) -> str:
    """Return the answer with every Caution and Note line checked against the
    manual text of the chunk(s) it was written from."""
    cautions = " ".join(c.get("cautions") or "" for c in chunks_used)
    notes = " ".join(c.get("notes") or "" for c in chunks_used)
    body = " ".join(f"{c.get('title') or ''} {c.get('content') or ''}" for c in chunks_used)

    out: list[str] = []
    removed = relabelled = 0
    for line in answer.split("\n"):
        m = _CALLOUT_RE.match(line)
        if not m or not m.group("body"):
            out.append(line)
            continue
        text = m.group("body")
        indent = re.match(r"\s*", m.group("lead")).group()   # keep nesting, drop a bullet
        is_caution = m.group("label").lower() in _CAUTION_LABELS
        in_cautions = _match(text, cautions) >= MIN_MATCH
        in_notes = _match(text, notes) >= MIN_MATCH or _match(text, body) >= MIN_MATCH
        if in_cautions:
            out.extend(callout_md("caution", text, indent))
            relabelled += not is_caution
        elif in_notes:
            out.extend(callout_md("note", text, indent))
            relabelled += is_caution
        else:
            removed += 1
            log.info("  removed a %s not found in the manual: %r", m.group("label"), text[:80])
    if removed or relabelled:
        log.info("  callouts: %d removed, %d relabelled", removed, relabelled)
    # Drop a blank line left doubled by a removal.
    return re.sub(r"\n{3,}", "\n\n", "\n".join(out)).strip()


def add_missing_callouts(answer: str, source: dict[str, Any] | None) -> str:
    """Append any caution or note of the source procedure the answer left out.

    Whether the model writes a procedure's note varies from run to run, and it
    sometimes writes a twin manual's wording, which check_callouts removes.
    Cautions are safety information, so the manual's own callouts are always
    shown, in the manual's words. One already covered anywhere in the answer
    (as a callout or within a step) is not repeated.
    """
    if not source:
        return answer
    missing: list[str] = []
    for kind, text in (("caution", source.get("cautions") or ""), ("note", source.get("notes") or "")):
        for item in (t.strip() for t in text.split("\n")):
            if not item or fallback_is_placeholder(item):
                continue
            if _match(item, answer) >= MIN_MATCH:
                continue
            missing.extend(callout_md(kind, item))
    if not missing:
        return answer
    log.info("  callouts: %d added from the manual", len([m for m in missing if m]))
    return re.sub(r"\n{3,}", "\n\n", answer.rstrip() + "\n" + "\n".join(missing)).strip()


def fallback_is_placeholder(text: str) -> bool:
    """A 'not yet documented' note is handled by the placeholder reply instead."""
    return "not yet documented" in text.lower() or "has not yet been written" in text.lower()


def focal_person_wording(answer: str) -> str:
    """Send users to the eAFYA Focal Person, not the System Administrator."""
    return _SYSADMIN_RE.sub(FOCAL_PERSON_PHRASE, answer)
