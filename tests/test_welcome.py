"""Tests for the chat greeting (app/ui/welcome.py).

    docker compose exec app pytest tests/test_welcome.py -v

  test_trial_note_only_where_switched_on
      The online trial shows the note first, as a message of its own (so it
      can have its own background); laptops and facilities don't show it.

  test_trial_note_wording
      The agreed wording: proof of concept, end date, facility installation,
      feedback to the eAFYA Focal Person.

  test_opening_words_match_the_page_script
      public/script.js finds the two messages by their opening words to give
      each its background; changing those words would silently lose them.
"""
from __future__ import annotations

import re
from pathlib import Path

from app.config import Settings
from app.ui import welcome


def test_trial_note_only_where_switched_on(monkeypatch) -> None:
    assert welcome.welcome_messages(trial_notice=True) == [welcome.TRIAL_NOTICE, welcome.GREETING]
    assert welcome.welcome_messages(trial_notice=False) == [welcome.GREETING]
    # Off unless a .env switches it on. Read without this machine's settings:
    # the online server's .env has TRIAL_NOTICE=true, which once failed this test there.
    monkeypatch.delenv("TRIAL_NOTICE", raising=False)
    assert Settings(_env_file=None).trial_notice is False


def test_trial_note_wording() -> None:
    note = welcome.TRIAL_NOTICE
    for phrase in ("proof of concept", "15 December", "installed at each health facility",
                   "eAFYA Focal Person"):
        assert phrase in note
    assert "faster" not in note and "patient names" not in note   # removed at the project lead's request


def test_opening_words_match_the_page_script() -> None:
    script = (Path(__file__).resolve().parents[1] / "public" / "script.js").read_text(encoding="utf-8")
    assert re.sub(r"\*", "", welcome.TRIAL_NOTICE).startswith("🧪 Trial version") and "Trial version" in script
    assert re.sub(r"\*", "", welcome.GREETING).startswith("👋 Hi! I'm the eAFYA Assistant")
    assert "Hi! I'm the eAFYA Assistant" in script
