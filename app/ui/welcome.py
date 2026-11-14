"""The greeting at the start of every chat.

On the online trial (askeafya.chat) a short note comes first, saying that the
site is a proof of concept and that the assistant itself will be installed at
each health facility. TRIAL_NOTICE=true in the server's .env switches it on;
laptops and facilities leave it out.
"""
from __future__ import annotations

from app.rag import fallback

TRIAL_NOTICE = (
    "🧪 **Trial version.** This online version of the eAFYA Assistant is a proof of concept "
    "for Ministry of Health stakeholders to test until **15 December**. The assistant itself "
    "will be installed at each health facility. Please send your feedback to the "
    f"**{fallback.FOCAL_PERSON}**."
)

GREETING = (
    "👋 Hi! I'm the **eAFYA Assistant**, your guide to eAFYA, the "
    "Ministry of Health EMR system.\n\n"
    "Ask me how to do anything from the user manuals, such as registering a "
    "patient, queueing them for a doctor, closing a clinic session, "
    "scheduling reminders or generating reports, and I'll walk you "
    "through the steps with the **screenshots** from the manual.\n\n"
    "ℹ️ I **don't** read patient data and I **don't** give clinical "
    "advice. For clinical questions, please consult a clinician or "
    "your clinical reference. For anything the manuals don't cover, "
    f"contact your facility's **{fallback.FOCAL_PERSON}**.\n\n"
    "Try one of the suggestions below, or ask anything in your own words."
)


def welcome_messages(*, trial_notice: bool) -> list[str]:
    """The opening messages, in order: the trial note (online trial only),
    then the greeting. Separate messages, so each gets its own light
    background (public/script.js tags them, public/style.css colours them)."""
    return [TRIAL_NOTICE, GREETING] if trial_notice else [GREETING]
