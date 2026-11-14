"""The suggested questions on the eAFYA Assistant welcome screen.

They appear as green buttons under the greeting (suggestion_actions); clicking
one asks it as if the user had typed it (app/main.py, on_suggestion). Chainlit's
own starter cards (get_starters) only show while a chat is empty, and the
greeting fills it at once, so they are never seen; they are kept for Chainlit
setups without a greeting.

Choose questions that are common, slightly different from each other, and
demonstrably answerable from the ingested manuals.
"""
from __future__ import annotations

import chainlit as cl

# (button label, question asked)
SUGGESTIONS: list[tuple[str, str]] = [
    ("Register a new patient", "How do I register a new patient?"),
    ("Close a clinic session", "A patient went home and I forgot to close their visit. What do I do?"),
    ("Queue a returning patient", "A returning patient already has a Patient Number — how do I queue them?"),
    ("Schedule a reminder", "How do I schedule a reminder for an appointment?"),
]
# public/script.js gives buttons whose label starts with this the green style.
SUGGESTION_MARK = "💬"

_ICONS = ["user-plus", "log-out", "list-plus", "bell"]


def get_starters() -> list[cl.Starter]:
    """Chainlit's starter cards, from the same suggestions."""
    return [
        cl.Starter(label=label, message=question, icon=f"/public/icons/{icon}.svg")
        for (label, question), icon in zip(SUGGESTIONS, _ICONS)
    ]


def suggestion_actions() -> list[cl.Action]:
    """One button per suggestion, shown under the greeting."""
    return [
        cl.Action(
            name="ask_suggestion",
            payload={"question": question},
            label=f"{SUGGESTION_MARK} {label}",
            tooltip=question,
        )
        for label, question in SUGGESTIONS
    ]
