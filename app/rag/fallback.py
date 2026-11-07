"""What the assistant says when it should NOT answer from the manuals.

Stakeholder rule (Oct 2026):
  - question outright outside eAFYA      -> "I only cover the eAFYA manuals"
  - about eAFYA but not in the manuals   -> "contact your eAFYA Focal Person"
  - anything we are unsure about         -> eAFYA Focal Person (the safe fallback)

Why this is decided in code rather than left to the generation model: a 3B
model handed five loosely-related chunks tends to improvise an answer instead
of admitting the manuals don't cover the question. So when retrieval comes
back WEAK (see RetrievalResult.is_strong) we skip generation entirely, ask the
model one short classification question instead (app/rag/classifier.py), and
reply with the fixed text in this module.

Measured on the 40-prompt test sheet, match strength alone cleanly separates
"answerable" from "not answerable", but it does NOT separate an undocumented
eAFYA question ("integrate eAFYA with DHIS2", 0.48) from an off-topic one
("what's the weather", 0.52) — hence the classifier for the weak band.
"""
from __future__ import annotations

import re

FOCAL_PERSON = "eAFYA Focal Person"

# --- Outcomes recorded against every assistant message (messages.outcome) ---
ANSWERED = "answered"
NOT_IN_MANUAL = "not_in_manual"      # about eAFYA, manuals don't cover it -> the gap list
OFF_TOPIC = "off_topic"
CLINICAL = "clinical"
PATIENT_DATA = "patient_data"
ABOUT_ASSISTANT = "about_assistant"
CLARIFY = "clarify"                  # vague question: asked which topic was meant
TROUBLESHOOT = "troubleshoot"        # "X is not working": Focal Person + X's steps
FILLER = "filler"
ERROR = "error"

# --- Fixed replies ----------------------------------------------------------
# NOT_COVERED_REPLY and the two *_DECLINE sentences are quoted verbatim in the
# system prompt, so the generation model produces them when it declines on its
# own; detect_outcome() keys off them. The user never sees a bare *_DECLINE:
# main.py swaps in the fuller *_REPLY, which says who the assistant is and
# what it CAN do instead (stakeholder request, Nov 2026).
NOT_COVERED_REPLY = (
    "I couldn't find this in the eAFYA user manuals. "
    f"Please contact your facility's **{FOCAL_PERSON}**."
)
CLINICAL_DECLINE = "I can't help with clinical questions."
PATIENT_DATA_DECLINE = "I can't look up patient records."
CLINICAL_REPLY = (
    "I'm the **eAFYA Assistant** — I'm here to help you use eAFYA, so I can't "
    "help with clinical questions such as diagnosis, treatment or dosing. "
    "Please consult a clinician or your clinical reference.\n\n"
    "I can show you how to record clinical information in eAFYA, for example: "
    "*How do I add a prescription?*"
)
PATIENT_DATA_REPLY = (
    "I'm the **eAFYA Assistant** — I'm here to help you use eAFYA. I don't "
    "have access to patient records or live figures, so I can't look up "
    "patient records for you.\n\n"
    "I can show you where to find this in eAFYA yourself, for example: "
    "*How do I view patient admissions?*"
)
OFF_TOPIC_REPLY = (
    "I'm the **eAFYA Assistant** — I only cover how to use eAFYA, from the "
    "official user manuals, so I can't help with that one.\n\n"
    "Try asking about a task in eAFYA, for example: *How do I register a patient?*"
)
STUB_REPLY = (
    "**{title}** is available in eAFYA but is not yet documented in the user "
    f"manuals. Please contact your facility's **{FOCAL_PERSON}**."
)
ERROR_REPLY = (
    "Something went wrong while preparing an answer. Please try again — if it "
    f"keeps happening, contact your facility's **{FOCAL_PERSON}**."
)


def about_reply(modules: list[str]) -> str:
    """Identity blurb, listing the manuals actually loaded."""
    lines = [
        "I'm the **eAFYA Assistant**. I explain how to use eAFYA, step by step, "
        "with screenshots from the official user manuals.",
    ]
    if modules:
        lines.append("\nI currently cover these modules:\n")
        lines.extend(f"- {m}" for m in modules)
    lines.append(
        "\nI don't read patient data and I don't give clinical advice. For "
        f"anything the manuals don't cover, your facility's **{FOCAL_PERSON}** can help."
    )
    return "\n".join(lines)


def fallback_reply(label: str, modules: list[str]) -> tuple[str, str]:
    """Map a classifier label to (reply text, outcome)."""
    if label == "OTHER":
        return OFF_TOPIC_REPLY, OFF_TOPIC
    if label == "CLINICAL":
        return CLINICAL_REPLY, CLINICAL
    if label == "PATIENT_DATA":
        return PATIENT_DATA_REPLY, PATIENT_DATA
    if label == "ASSISTANT":
        return about_reply(modules), ABOUT_ASSISTANT
    return NOT_COVERED_REPLY, NOT_IN_MANUAL


# --- Outcome of a generated answer ------------------------------------------
STUB_MARKER = "not yet documented"

# How a generated answer sounds when it is relaying one of the manuals'
# placeholders: "not yet documented", "has not yet been written", ...
_UNDOCUMENTED_RE = re.compile(r"\bnot (?:yet )?(?:been )?(?:documented|written)\b", re.IGNORECASE)

_NUMBERED_STEP_RE = re.compile(r"^\s*\d+[.)]\s", re.MULTILINE)


def detect_outcome(answer: str) -> str:
    """Classify an answer the generation model wrote, from the fixed sentences
    the system prompt tells it to use when it declines."""
    a = answer.lower()
    if "can't help with clinical" in a:
        return CLINICAL
    if "can't look up patient" in a:
        return PATIENT_DATA
    if "couldn't find this in the eafya" in a:
        return NOT_IN_MANUAL
    # A short reply that only points at the Focal Person is a decline. A full
    # set of steps that happens to end with "…or ask your Focal Person" is
    # still an answer and must keep its screenshots.
    if FOCAL_PERSON.lower() in a and len(a) < 400 and not _NUMBERED_STEP_RE.search(answer):
        return NOT_IN_MANUAL
    return ANSWERED


def cited_stub(answer: str, chunks: list[dict]) -> dict | None:
    """The placeholder chunk a generated answer is relaying, if any.

    Requires BOTH an "is not yet documented/written" phrase in the answer and
    a placeholder among the retrieved chunks — a placeholder merely ranking
    high must not hijack a real answer ("how do I dispense drugs?" retrieves
    the Bulk Dispensation placeholder first, yet is answered properly).
    """
    if not _UNDOCUMENTED_RE.search(answer):
        return None
    stubs = [c for c in chunks if is_stub(c)]
    if not stubs:
        return None
    a = answer.lower()
    named = [c for c in stubs if stub_title(c).lower() in a]
    return (named or stubs)[0]


def stub_title(chunk: dict) -> str:
    """'Bulk dispensation (not yet documented)' -> 'Bulk dispensation'."""
    title = (chunk.get("title") or "").strip()
    idx = title.lower().find(f"({STUB_MARKER}")
    return title[:idx].strip() if idx > 0 else title


def is_stub(chunk: dict) -> bool:
    """True for the honest 'not yet documented' placeholders in the manuals."""
    return (
        STUB_MARKER in (chunk.get("title") or "").lower()
        or STUB_MARKER in (chunk.get("content") or "").lower()
    )


# --- Chit-chat --------------------------------------------------------------
_CHITCHAT_WORDS = frozenset("""
    hi hello hey yo there ok okay k kk sure right alright got it cool nice great good fine
    morning afternoon evening yes yep yeah yup no nope thanks thank you thx ty cheers
    much very bye goodbye test testing then
""".split())


def is_chitchat(query: str) -> bool:
    """True for greetings, thanks and the like: messages that are not questions.

    Decided by vocabulary, not length: "billing" and "lab test" are short but
    they are questions about eAFYA.
    """
    words = re.findall(r"[a-z0-9+]+", query.lower())
    return all(w in _CHITCHAT_WORDS for w in words)
