"""Tests for vague questions and chit-chat (app/rag/topics.py, fallback.is_chitchat).

Pure functions — no database, no Ollama, no Chainlit:
    docker compose exec app pytest tests/test_topics.py -v

What each test catches:

  test_one_meaningful_word_is_vague
      "billing", "how do I send": choices are offered, best match first.

  test_two_words_depend_on_the_gap
      "patient records" (topics neck and neck) asks back; "cancel appointment"
      (one clear winner) is answered.

  test_never_vague
      Longer questions, definitions, exact topic titles and literal button
      labels go straight to an answer.

  test_words_unknown_to_the_manuals_are_not_vague
      "weather" must reach the off-topic handling, not get a menu of topics.

  test_no_meaningful_words_gets_a_tip_not_a_menu
      "it is not working" has nothing to base a menu on.

  test_options_are_distinct_procedures_and_skip_placeholders
      A title appearing as a procedure, as index entries and in several manuals
      is offered once, as its best-scoring procedure; placeholders are left out.

  test_unrelated_search_results_are_not_offered
      "malaria" must get a phrasing tip, not a list of procedures that merely
      came back from the search.

  test_question_naming_a_topic_is_answered_despite_a_small_gap
      Seen on the test sheet: the stores tab question asked back needlessly.

  test_not_working_gets_focal_person_and_the_steps
      "reminder not working": the eAFYA Focal Person plus buttons to re-check
      the steps. The assistant is a guide, not the final authority.

  test_ordinary_questions_are_not_trouble
      "patient not paying, what happens?" is a real question, not a failure.

  test_billing_is_not_chitchat
      Short messages used to be treated as greetings by length, so "billing"
      and "lab test" were answered with "Hello!".
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pytest

from app.rag import fallback, topics


@dataclass
class Result:
    candidates: list[dict[str, Any]] = field(default_factory=list)
    top_vector: float = 0.7
    top_bm25: float = 1.0


def chunk(i: int, title: str, score: float, kind: str = "procedure", content: str = "Steps:\nClick.") -> dict[str, Any]:
    return {"id": i, "title": title, "kind": kind, "vector_score": score, "content": content, "manual": "Billing Module"}


BILLING = [
    chunk(1, "View a billing audit", 0.60),
    chunk(2, "View a billing audit", 0.58),
    chunk(3, "Add a bill allocation", 0.59),
    chunk(4, "Add imaging billables", 0.57),
    chunk(5, "Add a bill allocation", 0.55, kind="index_entry"),
]


@pytest.mark.parametrize("query", ["billing", "Billing?", "how do I bill", "help with billing", "the billing page"])
def test_one_meaningful_word_is_vague(query: str) -> None:
    asked = topics.clarification(query, Result(candidates=BILLING))
    assert asked is not None and asked.reply == topics.CLARIFY_REPLY
    assert [c["id"] for c in asked.options] == [1, 3, 4]


def test_two_words_depend_on_the_gap() -> None:
    close = [chunk(1, "View a patient's medical records", 0.65), chunk(2, "Record allergies", 0.64)]
    assert topics.clarification("patient records", Result(candidates=close)) is not None
    clear = [chunk(1, "Delete an appointment", 0.83), chunk(2, "Set an appointment", 0.65)]
    assert topics.clarification("How do I cancel an appointment?", Result(candidates=clear)) is None


@pytest.mark.parametrize(
    "query",
    [
        "How do I approve a leave request?",            # three meaningful words
        "What is billing?",                             # definition
        "What does +New do?",                           # definition
        "+New",                                         # literal button label
        "add a bill allocation",                        # names a topic exactly
        "how do i add a bill allocation",
        "what can you help me with?",                   # about the assistant
    ],
)
def test_never_vague(query: str) -> None:
    assert topics.clarification(query, Result(candidates=BILLING)) is None


def test_words_unknown_to_the_manuals_are_not_vague() -> None:
    assert topics.clarification("weather", Result(candidates=BILLING, top_vector=0.45, top_bm25=0.0)) is None
    # A paraphrase with no keyword hit but a good meaning match is about eAFYA.
    assert topics.clarification("invoicing", Result(candidates=BILLING, top_vector=0.60, top_bm25=0.0)) is not None


@pytest.mark.parametrize("query", ["it is not working", "the button", "how do I use the system"])
def test_no_meaningful_words_gets_a_tip_not_a_menu(query: str) -> None:
    asked = topics.clarification(query, Result(candidates=BILLING))
    assert asked is not None and asked.options == []
    assert fallback.FOCAL_PERSON in asked.reply


def test_no_real_choice_is_answered_normally() -> None:
    assert topics.clarification("audit", Result(candidates=[chunk(1, "View a billing audit", 0.6)])) is None


def test_unrelated_search_results_are_not_offered() -> None:
    # Search returns procedures for any word; none of these is about "malaria".
    asked = topics.clarification("malaria", Result(candidates=BILLING))
    assert asked is not None and asked.options == [] and asked.reply == topics.CLARIFY_NO_TOPICS_REPLY


def test_related_means_title_or_manual() -> None:
    cands = [chunk(1, "Add a drug", 0.6), chunk(2, "Add a bed", 0.6), chunk(3, "Delete a contact", 0.7)]
    cands[1]["manual"] = "Pharmacy Management Module"
    cands[2]["manual"] = "Client Relationship Management Module"
    asked = topics.clarification("drugs", Result(candidates=cands[:1] + [chunk(4, "Add a drug category", 0.5)]))
    assert asked is not None and [c["id"] for c in asked.options] == [1, 4]
    asked = topics.clarification("pharmacy", Result(candidates=[chunk(5, "Add a pharmacology", 0.6), *cands]))
    assert asked is not None and [c["id"] for c in asked.options] == [5, 2]


def test_question_naming_a_topic_is_answered_despite_a_small_gap() -> None:
    cands = [chunk(1, "Use the stores tab", 0.635), chunk(2, "Dispense drugs to a patient", 0.61)]
    assert topics.clarification("How do I use the stores tab in the pharmacy?", Result(candidates=cands)) is None


def test_options_are_distinct_procedures_and_skip_placeholders() -> None:
    stub = chunk(6, "Bulk dispensation (not yet documented)", 0.9)
    index_only = chunk(7, "Only in the quick index", 0.8, kind="index_entry")
    options = topics.topic_options([stub, index_only, *BILLING])
    assert [c["id"] for c in options] == [1, 3, 4]
    assert [c["id"] for c in topics.topic_options(BILLING, limit=1)] == [1]
    assert topics.topic_options(BILLING, min_score=0.7) == []


def test_index_entry_score_lifts_its_procedure() -> None:
    cands = [chunk(1, "Add a drug", 0.50), chunk(2, "Add a drug", 0.70, kind="index_entry"), chunk(3, "Add a bed", 0.60)]
    assert [c["id"] for c in topics.topic_options(cands)] == [1, 3]


@pytest.mark.parametrize("query", ["billing", "queue", "lab test", "leave", "report", "asdf", "who are you?"])
def test_billing_is_not_chitchat(query: str) -> None:
    assert not fallback.is_chitchat(query)


@pytest.mark.parametrize("query", ["hi", "Hello!", "ok then", "thanks", "thank you very much", "good morning", "?", ""])
def test_chitchat(query: str) -> None:
    assert fallback.is_chitchat(query)


# --- "X is not working" -------------------------------------------------------

REMINDERS = [
    chunk(11, "Schedule an appointment reminder", 0.70),
    chunk(12, "Send a reminder", 0.69),
    chunk(13, "Remove a reminder", 0.65),
    chunk(14, "View a billing audit", 0.60),
]


@pytest.mark.parametrize("query", [
    "reminder not working",
    "the reminder is not sending",
    "reminders are not going through",
    "why can't I send a reminder?",
    "I get an error when sending a reminder",
    "reminder button is greyed out",
    "my reminder won't send",
])
def test_not_working_gets_focal_person_and_the_steps(query: str) -> None:
    out = topics.troubleshooting(query, Result(candidates=REMINDERS))
    assert out is not None and out.reply == topics.TROUBLE_REPLY
    assert fallback.FOCAL_PERSON in out.reply
    assert [c["id"] for c in out.options] == [11, 12]          # related only, at most two


@pytest.mark.parametrize("query", [
    "How do I send a reminder?",
    "patient not paying, what happens?",
    "what happens if a patient doesn't pay?",
    "The newborn is not doing well and requires NICU admission",
    "the system shows zero stock for that drug",
    "How do I issue drugs from the store?",
    "How do I add a missing drug to the list?",
    "How do I record a patient's problems and complaints?",
    "How do I correct an error in a patient record?",
])
def test_ordinary_questions_are_not_trouble(query: str) -> None:
    assert topics.troubleshooting(query, Result(candidates=REMINDERS)) is None


@pytest.mark.parametrize("query", ["it is not working", "page is not loading", "my patient can't breathe"])
def test_trouble_without_a_task_still_names_the_focal_person(query: str) -> None:
    # No task identified: no buttons, but the Focal Person. (The chat handler
    # first sends clinical / off-topic messages to their own replies.)
    out = topics.troubleshooting(query, Result(candidates=REMINDERS))
    assert out is not None and out.options == [] and out.reply == topics.TROUBLE_NO_TASK_REPLY
    assert fallback.FOCAL_PERSON in out.reply


def test_failure_words_do_not_pick_the_task() -> None:
    # "not delivered" must not offer "Enter delivery details".
    cands = [chunk(41, "Enter delivery details", 0.6), chunk(42, "Send a queued message", 0.55)]
    assert topics.troubleshooting("sms not delivered", Result(candidates=cands)).options == []


def test_clinical_trouble_is_not_an_eafya_problem() -> None:
    cands = [chunk(21, "Recommend a patient for admission", 0.6), chunk(22, "Register a patient", 0.58)]
    # No procedure is offered for it; the chat handler gives the clinical reply.
    assert topics.troubleshooting("my patient can't breathe", Result(candidates=cands)).options == []
    assert topics.troubleshooting("why can't I register a patient", Result(candidates=cands)) is not None


def test_more_failure_verbs() -> None:
    cands = [chunk(31, "Post an invoice and settle a payment", 0.7), chunk(32, "View a billing audit", 0.6)]
    assert topics.troubleshooting("the invoice won't post", Result(candidates=cands)) is not None
