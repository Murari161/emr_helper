"""Accounts: password hashing, temporary passwords, input rules, login lockout.

Stakeholder item 5: one login per person, each with a role.
  - user        everything except the restricted manuals (app/rag/access.py)
  - super_user  everything, and manages accounts at /admin (app/ui/admin.py)

Passwords are stored only as scrypt hashes (Python's standard library, no
extra dependency). A new or reset account gets a one-time temporary password
and must choose its own at /account before asking questions.

Pure functions and one small in-memory class — no database, no Chainlit — so
everything here is unit-testable (tests/test_auth.py).
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import re
import secrets
import time

USER = "user"
SUPER_USER = "super_user"
ROLES = (USER, SUPER_USER)
ROLE_LABELS = {USER: "User", SUPER_USER: "Super user"}

# At a facility installation without logins (LOGIN_REQUIRED=false), everyone
# who has not logged in uses the assistant under this name, with the
# ordinary user's limits. No real account may take it.
GUEST = "guest"
RESERVED_USERNAMES = frozenset({GUEST})

MIN_PASSWORD_LENGTH = 8
_USERNAME_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{2,39}$")

# scrypt cost: about 16 MB of memory and a few tens of milliseconds per check —
# cheap for one login, expensive for someone trying millions of guesses.
_N, _R, _P, _DKLEN = 2**14, 8, 1, 32


def _b64(raw: bytes) -> str:
    return base64.b64encode(raw).decode("ascii")


def hash_password(password: str) -> str:
    """'scrypt$N$r$p$salt$hash' — salt is random per password."""
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode("utf-8"), salt=salt, n=_N, r=_R, p=_P, dklen=_DKLEN)
    return f"scrypt${_N}${_R}${_P}${_b64(salt)}${_b64(digest)}"


def verify_password(password: str, stored: str) -> bool:
    """True if `password` matches the stored hash. Never raises."""
    try:
        algo, n, r, p, salt, digest = stored.split("$")
        if algo != "scrypt":
            return False
        expected = base64.b64decode(digest)
        actual = hashlib.scrypt(
            password.encode("utf-8"), salt=base64.b64decode(salt),
            n=int(n), r=int(r), p=int(p), dklen=len(expected),
        )
    except Exception:
        return False
    return hmac.compare_digest(actual, expected)


# Checked when the username does not exist, so a wrong username takes as long
# as a wrong password and does not reveal which usernames exist.
DUMMY_HASH = hash_password(secrets.token_hex(16))

# No 0/o, 1/l/i: temporary passwords are read out or typed from a message.
_TEMP_ALPHABET = "abcdefghjkmnpqrstuvwxyz23456789"


def temporary_password(length: int = 10) -> str:
    return "".join(secrets.choice(_TEMP_ALPHABET) for _ in range(length))


def normalise_username(username: str) -> str:
    return (username or "").strip().lower()


def username_problem(username: str) -> str | None:
    """Why a (normalised) username is not acceptable, or None."""
    if not _USERNAME_RE.match(username):
        return ("Usernames are 3 to 40 characters: lower-case letters, numbers, "
                "and . _ - (starting with a letter or number).")
    if username in RESERVED_USERNAMES:
        return f"The username {username!r} is reserved. Please choose another."
    return None


def password_problem(username: str, new: str, confirm: str, current: str | None = None) -> str | None:
    """Why a new password is not acceptable, or None."""
    if new != confirm:
        return "The two new passwords do not match."
    if len(new) < MIN_PASSWORD_LENGTH:
        return f"The new password must be at least {MIN_PASSWORD_LENGTH} characters."
    if new.strip().lower() == normalise_username(username):
        return "The new password must not be your username."
    if current is not None and new == current:
        return "The new password must be different from the current one."
    return None


def role_label(role: str) -> str:
    return ROLE_LABELS.get(role, role)


class LoginGuard:
    """Locks a username for a while after repeated wrong passwords.

    In memory, per app process: enough to stop password guessing on the proof
    of concept. A restart clears it.
    """

    def __init__(self, max_failures: int = 5, window_s: float = 15 * 60, lock_s: float = 15 * 60):
        self.max_failures = max_failures
        self.window_s = window_s
        self.lock_s = lock_s
        self._failures: dict[str, list[float]] = {}
        self._locked_until: dict[str, float] = {}

    def is_locked(self, username: str, now: float | None = None) -> bool:
        now = time.monotonic() if now is None else now
        until = self._locked_until.get(username)
        if until is None:
            return False
        if now >= until:
            del self._locked_until[username]
            return False
        return True

    def failed(self, username: str, now: float | None = None) -> None:
        now = time.monotonic() if now is None else now
        recent = [t for t in self._failures.get(username, []) if now - t < self.window_s]
        recent.append(now)
        if len(recent) >= self.max_failures:
            self._locked_until[username] = now + self.lock_s
            recent = []
        self._failures[username] = recent

    def succeeded(self, username: str) -> None:
        self._failures.pop(username, None)


# One lock for every way of logging in: Chainlit's login page and, at a
# facility installation, the super-user login at /account/login.
login_guard = LoginGuard()
