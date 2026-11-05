"""Tests for the fixed-reply logic in app/rag/fallback.py and classifier.py.

These cover only the pure functions — no database, no Ollama — so they run
anywhere:
    docker compose exec app pytest tests/test_fallback.py -v

What each group catches:

  test_parse_label_*
      The classifier's raw reply is mapped to a label even when the model adds
      punctuation or a trailing explanation; garbage maps to None so the
      caller falls back to the eAFYA Focal Person reply.

  test_about_the_assistant_shortcut
      "Who are you?"-style questions are caught before the model is asked,
      without swallowing real eAFYA questions that contain the word "modules".

  test_fallback_reply_*
      Every label produces the reply and outcome the stakeholders asked for;
      an unknown label is treated as "not in the manuals" (Focal Person).

  test_detect_outcome_*
      A generated answer is tagged from the decline sentences in the system
      prompt. The important regression: a full set of steps that merely
      mentions the Focal Person must stay 'answered', or it loses its
      screenshots.

  test_stub_*
      "(not yet documented)" placeholders are recognised and their title is
      cleaned for the fixed reply.
"""
from __future__ import annotations

import pytest

from app.rag import classifier, fallback


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("EAFYA", "EAFYA"),
        (" clinical\n", "CLINICAL"),
        ("PATIENT_DATA.", "PATIENT_DATA"),
        ("Patient data", "PATIENT_DATA"),
        ("OTHER - this is about the weather", "OTHER"),
        ("I think this is a joke", None),
        ("", None),
    ],
)
def test_parse_label(raw: str, expected: str | None) -> None:
    assert classifier._parse_label(raw) == expected


@pytest.mark.parametrize(
    ("query", "is_about"),
    [
        ("who are you?", True),
        ("what can you help me with?", True),
        ("what modules or topics do you know about?", True),
        ("Are you a real doctor?", True),
        ("How do I integrate eAFYA with DHIS2?", False),
        ("What do you know about dispensing drugs?", False),
        ("Which modules does the pharmacist use?", False),
    ],
)
def test_about_the_assistant_shortcut(query: str, is_about: bool) -> None:
    assert bool(classifier._ABOUT_RE.search(query)) is is_about


@pytest.mark.parametrize(
    ("label", "outcome", "must_contain"),
    [
        ("EAFYA", fallback.NOT_IN_MANUAL, fallback.FOCAL_PERSON),
        ("OTHER", fallback.OFF_TOPIC, "only cover how to use eAFYA"),
        ("CLINICAL", fallback.CLINICAL, "I'm here to help you use eAFYA"),
        ("PATIENT_DATA", fallback.PATIENT_DATA, "I'm here to help you use eAFYA"),
        ("ASSISTANT", fallback.ABOUT_ASSISTANT, "eAFYA Assistant"),
        ("SOMETHING_NEW", fallback.NOT_IN_MANUAL, fallback.FOCAL_PERSON),
    ],
)
def test_fallback_reply(label: str, outcome: str, must_contain: str) -> None:
    reply, got = fallback.fallback_reply(label, ["Billing Module"])
    assert got == outcome
    assert must_contain in reply


def test_about_reply_lists_the_loaded_manuals() -> None:
    reply = fallback.about_reply(["Billing Module", "Nursing Module"])
    assert "- Billing Module" in reply
    assert "- Nursing Module" in reply


def test_detect_outcome_recognises_the_fixed_declines() -> None:
    # What the model is told to say, and the fuller text the user is shown.
    assert fallback.detect_outcome(fallback.CLINICAL_DECLINE) == fallback.CLINICAL
    assert fallback.detect_outcome(fallback.CLINICAL_REPLY) == fallback.CLINICAL
    assert fallback.detect_outcome(fallback.PATIENT_DATA_DECLINE) == fallback.PATIENT_DATA
    assert fallback.detect_outcome(fallback.PATIENT_DATA_REPLY) == fallback.PATIENT_DATA
    assert fallback.detect_outcome(fallback.NOT_COVERED_REPLY) == fallback.NOT_IN_MANUAL


def test_detect_outcome_short_focal_person_pointer_is_a_decline() -> None:
    answer = "That is not described here. Please contact your eAFYA Focal Person."
    assert fallback.detect_outcome(answer) == fallback.NOT_IN_MANUAL


def test_detect_outcome_steps_mentioning_focal_person_stay_answered() -> None:
    answer = (
        "To register a patient:\n"
        "1. Click **+New**.\n"
        "2. Fill in the patient details.\n"
        "3. Click **Save**.\n\n"
        "If the form will not save, contact your eAFYA Focal Person."
    )
    assert fallback.detect_outcome(answer) == fallback.ANSWERED


def test_detect_outcome_plain_answer() -> None:
    assert fallback.detect_outcome("A debtor tracking account records what a client owes.") == fallback.ANSWERED


def test_stub_detection_and_title() -> None:
    stub = {"title": "Bulk dispensation (not yet documented)", "content": "How do I do a bulk dispensation?"}
    body_stub = {"title": "Roll back an admission", "content": "Note: This procedure is not yet documented in this manual."}
    real = {"title": "Dispense drugs to a patient", "content": "1. Open the Pharmacy module."}
    assert fallback.is_stub(stub)
    assert fallback.is_stub(body_stub)
    assert not fallback.is_stub(real)
    assert fallback.stub_title(stub) == "Bulk dispensation"
    assert fallback.stub_title(body_stub) == "Roll back an admission"


def test_cited_stub_needs_both_the_phrase_and_a_placeholder() -> None:
    stub = {"title": "Financial Statements (not yet documented)", "content": "Where are the financial statements?"}
    other_stub = {"title": "Bulk dispensation (not yet documented)", "content": "How do I do a bulk dispensation?"}
    real = {"title": "Dispense drugs to a patient", "content": "1. Open the Pharmacy module."}

    relaying = "The Financial Statements guide has not yet been written in this manual."
    assert fallback.cited_stub(relaying, [real, other_stub, stub]) is stub      # the named one wins
    assert fallback.cited_stub(relaying, [real]) is None                        # no placeholder retrieved
    # A real answer must not be hijacked by a placeholder that merely ranked high.
    assert fallback.cited_stub("1. Open the Pharmacy module.\n2. Click **Dispense**.", [other_stub, real]) is None
