"""Tests for keeping Caution and Note lines true to the manual (app/rag/callouts.py).

Pure functions — no database, no Ollama:
    docker compose exec app pytest tests/test_callouts.py -v

What each test catches (the first two were seen in stakeholder testing):

  test_invented_caution_is_removed
      "Add a bill allocation" has no caution or note; an invented
      "Ensure you have entered accurate billing information…" must go.

  test_note_shown_as_caution_is_relabelled
      The leave-approval Note must be shown as a Note, not a Caution.

  test_real_caution_is_kept
      A caution that is in the manual stays, in the standard format, even
      when the model wrote it with different markup.

  test_note_drawn_from_a_step_is_kept
      The model may phrase a step as a Note ("fields in red are mandatory").

  test_leaked_chunk_reference_is_removed
      "(Note: This step is from CHUNK [2])" is internal and must go.

  test_ordinary_lines_untouched

  test_system_administrator_becomes_focal_person

  test_missing_note_is_added_from_the_manual
      Seen live: the HR leave note vanished because the model wrote the
      Employee Portal's wording instead. The manual's own note must appear.
"""
from __future__ import annotations

import pytest

from app.rag import callouts

BILL = {
    "title": "Add a bill allocation", "cautions": "", "notes": "",
    "content": "Steps:\nGo to the Bed/Bill Allocation tab.\nSearch the patient using the Patient Number.\n"
               "Click the Add Billable Item button.\nClick +Add and enter the billing date, quantity and item.\n"
               "Click the Update button.",
}
LEAVE = {
    "title": "Approve a leave request", "cautions": "",
    "notes": "Employees raise the request in the Employee Portal module. Approval is governed by an approval "
             "mandate — contact the System Administrator (configured in User and Facility Management).",
    "content": "Steps:\nGo to Leave Management and click the Leave Request tab.\nClick the Approve Leave button.",
}
BED = {
    "title": "Change a patient's bed",
    "cautions": "Changing a bed is irreversible: once you click Yes the patient cannot be moved back automatically.",
    "notes": "",
    "content": "Steps:\nGo to the Bed/Bill Allocation tab.\nFields marked in red are mandatory.\nClick Change Bed.",
}

STEPS = "To add a bill allocation:\n\n1. Go to the **Bed/Bill Allocation** tab.\n2. Click the **Update** button."


def test_invented_caution_is_removed() -> None:
    answer = STEPS + "\n\n⚠️ Caution: Ensure you have entered accurate billing information to avoid any " \
                     "discrepancies in patient records."
    out = callouts.check_callouts(answer, [BILL])
    assert "Caution" not in out
    assert out == STEPS


def test_note_shown_as_caution_is_relabelled() -> None:
    answer = "1. Click the **Approve Leave** button.\n\n⚠️ Caution: Approval is governed by an approval mandate — " \
             "contact the System Administrator (configured in User and Facility Management)."
    out = callouts.check_callouts(answer, [LEAVE])
    assert "Caution" not in out
    assert "**Note:** Approval is governed by an approval mandate" in out


@pytest.mark.parametrize("line", [
    "⚠️ Caution: Changing a bed is irreversible once you click Yes.",
    "**Caution:** changing a bed is irreversible once you click Yes",
    "- ⚠️ **Caution**: Changing a bed is irreversible once you click Yes.",
    "Note: Changing a bed is irreversible once you click Yes.",          # a caution shown as a note
])
def test_real_caution_is_kept(line: str) -> None:
    out = callouts.check_callouts("1. Click **Change Bed**.\n\n" + line, [BED])
    assert "⚠️ **Caution:** " in out and "irreversible" in out


def test_note_drawn_from_a_step_is_kept() -> None:
    out = callouts.check_callouts("1. Fill in the form.\n\nNote: Fields marked in red are mandatory.", [BED])
    assert out.endswith("**Note:** Fields marked in red are mandatory.")


def test_leaked_chunk_reference_is_removed() -> None:
    out = callouts.check_callouts("1. Click **Update**.\n(Note: This step is from CHUNK [2].)", [BILL])
    assert "CHUNK" not in out and "Note" not in out


def test_ordinary_lines_untouched() -> None:
    answer = "To change a bed:\n\n1. Click **Change Bed**. Note that the list refreshes.\n2. Click **Yes**."
    assert callouts.check_callouts(answer, [BED]) == answer


@pytest.mark.parametrize("text", [
    "contact the System Administrator.",
    "contact your system administrator.",
    "contact the **System Administrator**.",
    "ask the system admin.",
])
def test_system_administrator_becomes_focal_person(text: str) -> None:
    out = callouts.focal_person_wording(text)
    assert "dministrator" not in out and "admin." not in out
    assert "your facility's **eAFYA Focal Person**" in out


def test_other_administrator_roles_untouched() -> None:
    text = "Log in with the Administrator role."
    assert callouts.focal_person_wording(text) == text


def test_missing_note_is_added_from_the_manual() -> None:
    # Seen live: the model wrote the Employee Portal's wording, which was
    # removed, so the HR procedure's own note must still appear.
    answer = "1. Click the **Approve Leave** button.\n2. Click **Close**."
    out = callouts.add_missing_callouts(answer, LEAVE)
    assert out.startswith(answer)
    assert out.endswith("**Note:** " + LEAVE["notes"])


def test_missing_caution_is_added_and_present_one_not_repeated() -> None:
    answer = "1. Click **Change Bed**."
    out = callouts.add_missing_callouts(answer, BED)
    assert out.count("Caution") == 1 and "irreversible" in out
    assert callouts.add_missing_callouts(out, BED) == out           # already there: unchanged


def test_nothing_added_when_the_manual_has_no_callouts() -> None:
    assert callouts.add_missing_callouts(STEPS, BILL) == STEPS
    assert callouts.add_missing_callouts(STEPS, None) == STEPS


def test_placeholder_note_is_not_added() -> None:
    stub = {"cautions": "", "notes": "This procedure is not yet documented in this manual."}
    assert callouts.add_missing_callouts(STEPS, stub) == STEPS


def test_focal_person_swap_keeps_spacing_and_drops_the_mandate_bracket() -> None:
    text = ("Approval is governed by an approval mandate — contact the System Administrator "
            "(configured in User and Facility Management).")
    assert callouts.focal_person_wording(text) == (
        "Approval is governed by an approval mandate — contact your facility's **eAFYA Focal Person**.")
    assert callouts.focal_person_wording("Ask the System Administrator now.") == (
        "Ask your facility's **eAFYA Focal Person** now.")


def test_callouts_are_their_own_blockquote() -> None:
    # A blockquote line straight after a step, or with text straight after it,
    # would merge with that text; each callout must stand alone.
    answer = "1. Click **Change Bed**.\nCaution: Changing a bed is irreversible once you click Yes.\nMore text."
    lines = callouts.check_callouts(answer, [BED]).split("\n")
    i = next(n for n, line in enumerate(lines) if line.startswith("> ⚠️ **Caution:**"))
    assert lines[i - 1] == "" and lines[i + 1] == ""
    out = callouts.add_missing_callouts("1. Click **Approve Leave**.", LEAVE)
    assert out.split("\n")[-1].startswith("> **Note:** ")
