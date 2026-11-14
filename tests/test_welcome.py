"""Tests for the chat greeting (app/ui/welcome.py).

    docker compose exec app pytest tests/test_welcome.py -v

  test_trial_note_only_where_switched_on
      The online trial shows the note first; laptops and facilities don't.

  test_trial_note_wording
      The agreed wording: proof of concept, end date, facility installation,
      feedback to the eAFYA Focal Person.
"""
from __future__ import annotations

from app.config import Settings
from app.ui import welcome


def test_trial_note_only_where_switched_on() -> None:
    with_note = welcome.welcome_message(trial_notice=True)
    assert with_note.startswith(welcome.TRIAL_NOTICE) and with_note.endswith(welcome.GREETING)
    assert welcome.welcome_message(trial_notice=False) == welcome.GREETING
    assert Settings().trial_notice is False                       # off unless the server's .env says so


def test_trial_note_wording() -> None:
    note = welcome.TRIAL_NOTICE
    for phrase in ("proof of concept", "15 December", "installed at each health facility",
                   "eAFYA Focal Person"):
        assert phrase in note
    assert "faster" not in note and "patient names" not in note   # removed at the project lead's request
