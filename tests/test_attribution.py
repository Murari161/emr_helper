"""Tests for picking the chunk an answer was written from (app/rag/attribution.py).

Pure functions — no database, no Ollama:
    docker compose exec app pytest tests/test_attribution.py -v

What each test catches:

  test_twin_title_does_not_steal_the_answer
      Two manuals document "send a patient for imaging". The answer reproduces
      one chunk's steps but opens with the other's title. The source must be
      the chunk whose steps were used — otherwise the user sees one manual's
      steps with the other manual's screenshots (seen live: "Imaging
      Investigation button" steps shown with the Doctor module's
      Investigations > Imaging screenshot).

  test_mentioned_procedure_is_not_the_source
      A procedure named in a closing note is not where the answer came from.

  test_no_source_when_nothing_matches
      A heavily paraphrased or unrelated answer returns None, so the caller
      falls back to its title heuristic.

  test_ties_go_to_the_higher_ranked_chunk
"""
from __future__ import annotations

from app.rag import attribution

DIRECT = {
    "title": "Send a patient for imaging (direct imaging test)",
    "content": "Steps:\nOn the registered patient's record, click the Imaging Investigation button in the patient toolbar.\n"
               "Under Referring Facility details, enter the facility that referred the patient.\n"
               "Click the +Add button.\nAdd the imaging test the patient has come for.\n"
               "Click Update to save the test, then click Close.\nDirect the patient to the imaging station.",
}
DOCTOR = {
    "title": "Send a patient for an imaging test",
    "content": "Steps:\nUnder Investigations, click the Imaging tab.\nClick the +Add Request button.\n"
               "Type the imaging request from the dropdown list.\nClick Update to save the record.",
}
ANSWER = (
    "To send a patient for an imaging test, follow these steps:\n\n"
    "1. On the registered patient's record, click the **Imaging Investigation** button in the patient toolbar.\n"
    "2. Under Referring Facility details, enter the facility that referred the patient.\n"
    "3. Click the **+Add** button.\n"
    "4. Add the imaging test the patient has come for.\n"
    "5. Click **Update** to save the test, then click **Close**.\n\n"
    "Direct the patient to the imaging station."
)


def test_twin_title_does_not_steal_the_answer() -> None:
    assert DOCTOR["title"].lower() in ANSWER.lower()             # the trap: the other title is quoted
    assert attribution.source_chunk(ANSWER, [DOCTOR, DIRECT]) is DIRECT
    assert attribution.source_chunk(ANSWER, [DIRECT, DOCTOR]) is DIRECT


def test_mentioned_procedure_is_not_the_source() -> None:
    queue = {"title": "Queue a returning patient",
             "content": "Click the Register Patient tab.\nSearch by Patient Number.\nClick Add to Queue."}
    answer = ANSWER + "\n\nNote: for a returning patient, follow Queue a returning patient instead."
    assert attribution.source_chunk(answer, [queue, DIRECT]) is DIRECT


def test_no_source_when_nothing_matches() -> None:
    assert attribution.source_chunk("Bake the bread for forty minutes until golden.", [DIRECT, DOCTOR]) is None
    assert attribution.source_chunk("", [DIRECT]) is None
    assert attribution.source_chunk(ANSWER, []) is None


def test_ties_go_to_the_higher_ranked_chunk() -> None:
    twin = dict(DIRECT)
    assert attribution.source_chunk(ANSWER, [twin, DIRECT]) is twin
