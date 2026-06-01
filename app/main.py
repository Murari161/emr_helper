"""Chainlit entry point for EMR Helper.

Phase 1 stub: confirms Chainlit starts, accepts messages, and echoes them back.
The real retrieval/generation pipeline lands in later phases (rag/* modules).
"""
from __future__ import annotations

import logging

import chainlit as cl

from app.config import settings

logging.basicConfig(level=settings.app_log_level)
log = logging.getLogger("emr_helper")


@cl.on_chat_start
async def on_chat_start() -> None:
    log.info("EMR Helper ready")
    await cl.Message(
        content=(
            "**EMR Helper** is online.\n\n"
            "I answer questions from the EMR user manuals — registration, "
            "queueing, appointments, reports, and so on. I do **not** read patient "
            "data and I do **not** give clinical advice."
        ),
    ).send()


@cl.on_message
async def on_message(message: cl.Message) -> None:
    # Phase 1 echo. Replaced in Phase 5 with retrieval + streaming generation.
    await cl.Message(content=f"(echo) {message.content}").send()
