"""Tests for passwords, usernames and the login lock (app/auth.py).

Pure functions — no database, no Chainlit:
    docker compose exec app pytest tests/test_auth.py -v

What each test catches:

  test_password_round_trip / test_hash_is_salted_and_hides_the_password
      Only the right password matches; two people with the same password get
      different hashes; the password is not recoverable from what is stored.

  test_verify_never_raises
      A damaged stored value is a failed login, not a crash on the login page.

  test_temporary_passwords
      Random, typeable (no 0/O, 1/l/I), and accepted by the password rules.

  test_login_guard_*
      Five wrong passwords lock that username for a while, for that username
      only; a correct password clears the count.
"""
from __future__ import annotations

import pytest

from app import auth


def test_password_round_trip() -> None:
    stored = auth.hash_password("correct horse")
    assert auth.verify_password("correct horse", stored)
    assert not auth.verify_password("Correct horse", stored)
    assert not auth.verify_password("", stored)


def test_hash_is_salted_and_hides_the_password() -> None:
    a, b = auth.hash_password("same password"), auth.hash_password("same password")
    assert a != b
    assert "same password" not in a


@pytest.mark.parametrize("stored", ["", "nonsense", "scrypt$x$y$z$a$b", "bcrypt$1$2$3$4$5", "$$$$$", "scrypt$16384$8$1$!!$!!"])
def test_verify_never_raises(stored: str) -> None:
    assert auth.verify_password("anything", stored) is False


def test_dummy_hash_matches_nothing_guessable() -> None:
    assert not auth.verify_password("", auth.DUMMY_HASH)
    assert not auth.verify_password("password", auth.DUMMY_HASH)


def test_temporary_passwords() -> None:
    seen = {auth.temporary_password() for _ in range(200)}
    assert len(seen) == 200
    for p in seen:
        assert len(p) == 10
        assert not set(p) & set("0oO1lIi")
        assert auth.password_problem("someone", p, p) is None


@pytest.mark.parametrize("raw,expected", [("  JNamukasa ", "jnamukasa"), ("ADMIN", "admin"), ("", ""), (None, "")])
def test_normalise_username(raw, expected) -> None:
    assert auth.normalise_username(raw) == expected


@pytest.mark.parametrize("name", ["abc", "j.namukasa", "nurse_01", "a-b-c", "x" * 40])
def test_good_usernames(name: str) -> None:
    assert auth.username_problem(name) is None


@pytest.mark.parametrize("name", ["", "ab", "x" * 41, "has space", "-start", ".start", "UPPER", "a/b", "<b>x</b>", "a@b.org"])
def test_bad_usernames(name: str) -> None:
    assert auth.username_problem(name) is not None


def test_guest_is_reserved() -> None:
    # At a facility installation everyone without a login is "guest"; a real
    # account of that name would inherit their sessions.
    assert "reserved" in auth.username_problem(auth.GUEST)


def test_one_lock_for_every_login() -> None:
    assert isinstance(auth.login_guard, auth.LoginGuard)


def test_password_rules() -> None:
    assert auth.password_problem("jane", "longenough", "different1") is not None     # must match
    assert auth.password_problem("jane", "short", "short") is not None               # too short
    assert auth.password_problem("janedoe1", "JaneDoe1", "JaneDoe1") is not None     # the username
    assert auth.password_problem("jane", "samesame1", "samesame1", current="samesame1") is not None
    assert auth.password_problem("jane", "a good one", "a good one", current="the old one") is None


def test_role_labels() -> None:
    assert auth.role_label(auth.SUPER_USER) == "Super user"
    assert auth.role_label(auth.USER) == "User"


def _guard() -> auth.LoginGuard:
    return auth.LoginGuard(max_failures=5, window_s=100, lock_s=50)


def test_login_guard_locks_after_repeated_failures() -> None:
    g = _guard()
    for t in range(4):
        g.failed("jane", now=t)
        assert not g.is_locked("jane", now=t)
    g.failed("jane", now=4)
    assert g.is_locked("jane", now=4)
    assert g.is_locked("jane", now=53)
    assert not g.is_locked("jane", now=54)        # the lock wears off
    g.failed("jane", now=55)
    assert not g.is_locked("jane", now=55)        # and the count starts again


def test_login_guard_is_per_username() -> None:
    g = _guard()
    for t in range(5):
        g.failed("jane", now=t)
    assert g.is_locked("jane", now=5)
    assert not g.is_locked("peter", now=5)


def test_login_guard_correct_password_clears_the_count() -> None:
    g = _guard()
    for t in range(4):
        g.failed("jane", now=t)
    g.succeeded("jane")
    g.failed("jane", now=5)
    assert not g.is_locked("jane", now=5)


def test_login_guard_forgets_old_failures() -> None:
    g = _guard()
    for t in range(4):
        g.failed("jane", now=t)
    g.failed("jane", now=500)                      # the first four are long past
    assert not g.is_locked("jane", now=500)
