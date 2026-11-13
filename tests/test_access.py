"""Tests for who may be answered from which manual (app/rag/access.py).

Pure functions — no database, no Ollama, no Chainlit:
    docker compose exec app pytest tests/test_access.py -v

What each test catches:

  test_excluded_manuals
      Super users see everything; anyone else, including an unknown role, gets
      the ordinary user's limits (fail closed).

  test_measured_questions
      Real scores from the live manuals. Restricted tasks are refused;
      "reset my password" and "apply for leave" are answered from the Employee
      Portal; "Add a referring facility" is in Patient Management too, so it
      is answered even though User & Facility Management scores higher.

  test_account_block_reply
      A disabled account, or one still on a temporary password, cannot ask.

  test_full_version_in_a_restricted_manual_is_not_used
      An open manual's summary must not be swapped for the restricted full
      version: that would hand the restricted steps to an ordinary user.
"""
from __future__ import annotations

import asyncio

import pytest

from app import auth
from app.config import Settings
from app.rag import access, canonical, fallback

HR = "Human Resources Management Module"
UF = "User and Facility Management Module"
RESTRICTED = [HR, UF]


@pytest.mark.parametrize(
    "role,expected",
    [(auth.SUPER_USER, []), (auth.USER, RESTRICTED), (None, RESTRICTED), ("something else", RESTRICTED)],
)
def test_excluded_manuals(role, expected) -> None:
    assert access.excluded_manuals(role, RESTRICTED) == expected


def test_is_allowed() -> None:
    assert access.is_allowed({"manual": "Billing Module"}, RESTRICTED)
    assert not access.is_allowed({"manual": HR}, RESTRICTED)
    assert not access.is_allowed(None, RESTRICTED)
    assert access.is_allowed({"manual": HR}, [])                     # super user


def test_restricted_reply_names_the_module_and_the_focal_person() -> None:
    reply = access.restricted_reply(UF)
    assert "User and Facility Management" in reply and "Module module" not in reply
    assert "limited to super users" in reply and fallback.FOCAL_PERSON in reply


def _match(manual, best, open_top, title=None, open_titles=()):
    return access.restricted_match(
        manual, best, open_top, strong=0.60, gap=0.03, best_title=title, open_titles=open_titles
    )


@pytest.mark.parametrize(
    "manual,best,open_top,title,open_titles,refused",
    [
        (UF, 0.718, 0.643, "Create a user", ["Reset your password"], True),      # create a user account
        (HR, 0.787, 0.685, "Add an employee", ["Add a contact"], True),          # add an employee
        (UF, 0.763, 0.610, "Add a branch", ["Add a bed"], True),                 # add a branch
        (HR, 0.648, 0.594, "Add a training", ["Add a course"], True),            # record an employee's training
        (UF, 0.595, 0.778, "Reset a password", ["Reset your password"], False),  # reset my password: too weak
        (HR, 0.743, 0.809, "Add leave days", ["Apply for leave"], False),        # apply for leave: open is better
        (HR, 0.563, 0.602, "Leave types", ["Apply for leave"], False),           # leave
        (HR, 0.851, 0.851, "Approve a leave request", ["Approve a leave request"], False),  # also in Employee Portal
        (UF, 0.840, 0.799, "Add a referring facility", ["Add a referring facility"], False),  # also in Patient Mgmt
    ],
)
def test_measured_questions(manual, best, open_top, title, open_titles, refused) -> None:
    assert _match(manual, best, open_top, title, open_titles) == (manual if refused else None)


def test_no_restricted_match() -> None:
    assert _match(None, 0.0, 0.5) is None


def test_same_title_ignores_case_and_brackets() -> None:
    assert _match(UF, 0.8, 0.6, "Add a Ward", ["add a ward (not yet documented)"]) is None


def test_nothing_open_to_compare_with() -> None:
    assert _match(UF, 0.8, 0.0, "Add a branch", []) == UF


def test_account_block_reply() -> None:
    ok = {"active": True, "must_change_password": False}
    assert access.account_block_reply(ok) is None
    assert access.account_block_reply(None) == access.NO_ACCOUNT_REPLY
    assert access.account_block_reply({**ok, "active": False}) == access.ACCOUNT_DISABLED_REPLY
    assert access.account_block_reply({**ok, "must_change_password": True}) == access.CHANGE_PASSWORD_REPLY
    # Disabled wins: no point asking a disabled account to choose a password.
    assert access.account_block_reply({"active": False, "must_change_password": True}) == access.ACCOUNT_DISABLED_REPLY


def test_guest_is_an_ordinary_user_who_can_ask() -> None:
    guest = access.GUEST_ACCOUNT
    assert guest["username"] == auth.GUEST and guest["role"] == auth.USER
    assert access.account_block_reply(guest) is None
    assert access.excluded_manuals(guest["role"], RESTRICTED) == RESTRICTED


def test_visible_modules() -> None:
    modules = ["Billing Module", HR, UF]
    assert access.visible_modules(modules, RESTRICTED) == (["Billing Module"], [HR, UF])
    assert access.visible_modules(modules, []) == (modules, [])


def test_restricted_modules_line() -> None:
    assert access.restricted_modules_line([]) == ""
    one = access.restricted_modules_line([HR])
    assert "Human Resources Management is limited to super users" in one
    two = access.restricted_modules_line([HR, UF])
    assert "Human Resources Management and User and Facility Management are limited" in two


def test_restricted_manuals_setting() -> None:
    assert Settings(restricted_modules=" A Module, B Module ,,").restricted_manuals == ["A Module", "B Module"]
    assert Settings().restricted_manuals == RESTRICTED


SUMMARY = {
    "id": 1, "kind": "procedure", "title": "Approve a thing", "manual": "Employee Portal Module",
    "content": "Approval is performed in the Human Resources module; it is summarised here.\n"
               "See module: Human Resources Management\nSteps:\nOpen the module.",
    "images": [],
}
FULL = {
    "id": 2, "kind": "procedure", "title": "Approve a thing", "manual": HR,
    "content": "Steps:\nClick Approve.\nEnter your password.", "images": [{"path": "x"}],
}


def _swap(monkeypatch, excluded):
    async def find(module_key, title):
        return FULL

    monkeypatch.setattr(canonical.queries, "find_procedure_in_module", find)
    return asyncio.run(canonical.prefer_full_versions([SUMMARY], excluded_manuals=excluded))


def test_full_version_in_a_restricted_manual_is_not_used(monkeypatch) -> None:
    assert [c["id"] for c in _swap(monkeypatch, RESTRICTED)] == [1]


def test_super_user_still_gets_the_full_version(monkeypatch) -> None:
    assert [c["id"] for c in _swap(monkeypatch, [])] == [2]
