"""Which manuals a role may be answered from (stakeholder item 5).

Ordinary users get no answers from the restricted manuals (Human Resources
and User & Facility Management "for now"; env RESTRICTED_MODULES). Super users
see everything.

How a restricted question is recognised: the search for an ordinary user
skips the restricted manuals, and a second, cheap lookup finds the best match
inside them. The question is about a restricted task, and gets
RESTRICTED_REPLY, only if that match is
  - strong, and
  - clearly better than anything in the open manuals, and
  - not also documented in an open manual under the same title.
Otherwise it is answered from the open manuals: "how do I reset my
password?" and "how do I apply for leave?" have good answers in the Employee
Portal, and "Add a referring facility" is a full procedure in Patient
Management as well as in User & Facility Management (measured in
RAG_modifications.md, M12).

Pure functions only, so they are unit-testable (tests/test_access.py).
"""
from __future__ import annotations

import re
from typing import Any, Iterable

from app.auth import SUPER_USER
from app.rag import fallback

RESTRICTED = "restricted"          # outcome logged in messages.outcome

RESTRICTED_REPLY = (
    "This task is in the **{module}** module, which is limited to super users. "
    f"Please contact your facility's **{fallback.FOCAL_PERSON}**."
)
RESTRICTED_MODULES_LINE = (
    "\n{modules} {verb} limited to super users. If you need help there, "
    f"contact your facility's **{fallback.FOCAL_PERSON}**."
)


ACCOUNT = "account"                # outcome: a question refused because of the account

CHANGE_PASSWORD_REPLY = (
    "🔑 Before you start, please choose your own password: "
    "**[Change your password](/account)**. Then come back here and ask your question."
)
ACCOUNT_DISABLED_REPLY = (
    "Your account has been disabled. If you think this is a mistake, please "
    f"contact your facility's **{fallback.FOCAL_PERSON}**."
)
NO_ACCOUNT_REPLY = (
    "I could not find your account. Please log out and log in again; if it keeps "
    f"happening, contact your facility's **{fallback.FOCAL_PERSON}**."
)


def account_block_reply(account: dict[str, Any] | None) -> str | None:
    """Why this account may not ask questions right now, or None if it may."""
    if account is None:
        return NO_ACCOUNT_REPLY
    if not account.get("active"):
        return ACCOUNT_DISABLED_REPLY
    if account.get("must_change_password"):
        return CHANGE_PASSWORD_REPLY
    return None


def excluded_manuals(role: str | None, restricted: Iterable[str]) -> list[str]:
    """Manuals this role must not be answered from. Unknown roles get the
    ordinary user's limits."""
    return [] if role == SUPER_USER else list(restricted)


def is_allowed(chunk: dict[str, Any] | None, excluded: Iterable[str]) -> bool:
    return chunk is not None and (chunk.get("manual") or "") not in set(excluded)


def module_name(manual: str) -> str:
    """'Human Resources Management Module' -> 'Human Resources Management'."""
    return re.sub(r"\s+Module$", "", (manual or "").strip())


def restricted_reply(manual: str) -> str:
    return RESTRICTED_REPLY.format(module=module_name(manual))


def _title(text: str | None) -> str:
    return re.sub(r"\s+", " ", re.sub(r"\(.*?\)", " ", (text or "").lower())).strip()


def restricted_match(
    best_manual: str | None,
    best_score: float,
    open_top_vector: float,
    *,
    strong: float,
    gap: float,
    best_title: str | None = None,
    open_titles: Iterable[str] = (),
) -> str | None:
    """The restricted manual a question is about, or None.

    best_manual / best_score / best_title: the best vector match inside the
    restricted manuals. open_top_vector: the best vector match in the open
    manuals. open_titles: titles of the open manuals' candidates.
    """
    if not best_manual or best_score < strong:
        return None
    if best_score - open_top_vector < gap:
        return None
    if best_title and _title(best_title) in {_title(t) for t in open_titles}:
        return None                     # the open manuals document the same task
    return best_manual


def visible_modules(modules: list[str], excluded: Iterable[str]) -> tuple[list[str], list[str]]:
    """(modules this role can ask about, restricted modules that exist)."""
    ex = set(excluded)
    return [m for m in modules if m not in ex], [m for m in modules if m in ex]


def restricted_modules_line(hidden: list[str]) -> str:
    """'Human Resources Management and User and Facility Management are limited…'"""
    if not hidden:
        return ""
    names = [module_name(m) for m in hidden]
    joined = names[0] if len(names) == 1 else ", ".join(names[:-1]) + " and " + names[-1]
    return RESTRICTED_MODULES_LINE.format(modules=joined, verb="is" if len(names) == 1 else "are")
