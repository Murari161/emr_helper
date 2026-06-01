"""Conversation starters shown on the EMR Helper welcome screen.

These appear as clickable suggestions when a user first opens the chat;
clicking one sends it as if the user had typed it. Choose questions that
are common, slightly different from each other, and demonstrably answerable
from the ingested manual.
"""
from __future__ import annotations

import chainlit as cl


def get_starters() -> list[cl.Starter]:
    """Return the four conversation starters shown to the user at chat-start."""
    return [
        cl.Starter(
            label="Register a new patient",
            message="How do I register a new patient?",
            icon="/public/icons/user-plus.svg",
        ),
        cl.Starter(
            label="Close a clinic session",
            message="A patient went home and I forgot to close their visit. What do I do?",
            icon="/public/icons/log-out.svg",
        ),
        cl.Starter(
            label="Queue a returning patient",
            message="A returning patient already has a Patient Number — how do I queue them?",
            icon="/public/icons/list-plus.svg",
        ),
        cl.Starter(
            label="Schedule a reminder",
            message="How do I schedule a reminder for an appointment?",
            icon="/public/icons/bell.svg",
        ),
    ]
