"""Tests for the account pages (app/ui/admin.py), with the database faked.

No Postgres or Ollama needed:
    docker compose exec app pytest tests/test_admin.py -v

What each test catches:

  test_not_logged_in_* / test_ordinary_user_*
      The pages and their forms do nothing for someone who is not logged in,
      or not a super user.

  test_add_account_*
      The temporary password appears once, only its hash is stored, and the
      new account must choose its own password.

  test_cannot_disable_or_demote_yourself
      A super user cannot lock themselves, and everyone else, out.

  test_forms_from_another_site_are_refused
      A page on another site cannot press these buttons for a logged-in super user.

  test_names_are_escaped
      A full name containing HTML is shown as text, not run in the admin's browser.

  test_pages_come_before_the_catch_all
      Chainlit answers every unknown address with the chat page; without the
      re-ordering, /admin would silently show the chat.

  test_login_required_wrapper
      Screenshots are only served to logged-in users.
"""
from __future__ import annotations

import re
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.responses import PlainTextResponse
from fastapi.testclient import TestClient

from app import auth
from app.ui import admin


class FakeDB:
    def __init__(self) -> None:
        self.users: dict[str, dict[str, Any]] = {}

    def add(self, username: str, *, role: str = auth.USER, password: str = "the-old-password",
            must_change: bool = False, active: bool = True, full_name: str = "") -> None:
        self.users[username] = dict(
            username=username, full_name=full_name, role=role, password_hash=auth.hash_password(password),
            must_change_password=must_change, active=active, last_login_at=None,
        )

    async def get_app_user(self, username: str):
        u = self.users.get(username)
        return dict(u) if u else None

    async def list_app_users(self):
        return [dict(u) for u in self.users.values()]

    async def create_app_user(self, *, username, full_name, role, password_hash, created_by) -> bool:
        if username in self.users:
            return False
        self.users[username] = dict(
            username=username, full_name=full_name, role=role, password_hash=password_hash,
            must_change_password=True, active=True, last_login_at=None,
        )
        return True

    async def set_app_user_password(self, username, password_hash, *, must_change, by) -> bool:
        if username not in self.users:
            return False
        self.users[username].update(password_hash=password_hash, must_change_password=must_change)
        return True

    async def set_app_user_role(self, username, role, *, by) -> bool:
        if username not in self.users:
            return False
        self.users[username]["role"] = role
        return True

    async def set_app_user_active(self, username, active, *, by) -> bool:
        if username not in self.users:
            return False
        self.users[username]["active"] = active
        return True


class Site:
    """A test client, a fake database, and who is currently logged in."""

    def __init__(self, monkeypatch) -> None:
        self.db = FakeDB()
        self.me: str | None = None
        monkeypatch.setattr(admin, "queries", self.db)
        monkeypatch.setattr(admin, "session_username", lambda request: self.me)
        app = FastAPI()
        app.include_router(admin.router)
        self.client = TestClient(app, follow_redirects=False)

    def login_as(self, username: str, **kwargs: Any) -> None:
        if username not in self.db.users:
            self.db.add(username, **kwargs)
        self.me = username

    def post(self, path: str, **data: str):
        return self.client.post(path, data=data)


@pytest.fixture
def site(monkeypatch) -> Site:
    return Site(monkeypatch)


def temp_password(html: str) -> str:
    m = re.search(r'class="secret">([^<]+)<', html)
    assert m, "no temporary password on the page"
    return m.group(1)


@pytest.mark.parametrize("path", ["/admin", "/account"])
def test_not_logged_in_is_sent_to_login(site: Site, path: str) -> None:
    r = site.client.get(path)
    assert r.status_code == 303 and r.headers["location"] == "/login"


def test_menu_is_told_who_is_logged_in(site: Site) -> None:
    assert site.client.get("/account/me").status_code == 401
    site.login_as("jane")
    r = site.client.get("/account/me")
    assert r.json() == {"username": "jane", "role": auth.USER} and r.headers["cache-control"] == "no-store"
    site.db.users["jane"]["role"] = auth.SUPER_USER             # a role change shows at once
    assert site.client.get("/account/me").json()["role"] == auth.SUPER_USER
    site.db.users["jane"]["active"] = False
    assert site.client.get("/account/me").status_code == 401


def test_not_logged_in_cannot_add_accounts(site: Site) -> None:
    r = site.post("/admin/accounts", username="intruder", role=auth.SUPER_USER)
    assert r.status_code == 303 and "intruder" not in site.db.users


def test_disabled_account_counts_as_logged_out(site: Site) -> None:
    site.login_as("jane", active=False)
    assert site.client.get("/account").headers["location"] == "/login"


def test_ordinary_user_cannot_open_admin(site: Site) -> None:
    site.login_as("jane")
    r = site.client.get("/admin")
    assert r.status_code == 403 and "for super users" in r.text


@pytest.mark.parametrize(
    "path,data",
    [
        ("/admin/accounts", {"username": "newone", "role": auth.SUPER_USER}),
        ("/admin/accounts/boss/reset", {}),
        ("/admin/accounts/jane/role", {"role": auth.SUPER_USER}),
        ("/admin/accounts/boss/active", {"active": "0"}),
    ],
)
def test_ordinary_user_cannot_use_admin_forms(site: Site, path: str, data: dict[str, str]) -> None:
    site.db.add("boss", role=auth.SUPER_USER)
    site.login_as("jane")
    before = {k: dict(v) for k, v in site.db.users.items()}
    assert site.post(path, **data).status_code == 403
    assert site.db.users == before


def test_super_user_on_a_temporary_password_must_change_it_first(site: Site) -> None:
    site.login_as("boss", role=auth.SUPER_USER, must_change=True)
    r = site.client.get("/admin")
    assert r.status_code == 303 and r.headers["location"] == "/account"


def test_add_account_shows_the_password_once_and_stores_only_a_hash(site: Site) -> None:
    site.login_as("boss", role=auth.SUPER_USER)
    r = site.post("/admin/accounts", username=" JNamukasa ", full_name="Jane  Namukasa", role=auth.USER)
    assert r.status_code == 200 and r.headers["cache-control"] == "no-store"
    password = temp_password(r.text)
    new = site.db.users["jnamukasa"]
    assert new["full_name"] == "Jane Namukasa" and new["role"] == auth.USER and new["must_change_password"]
    assert password not in new["password_hash"] and auth.verify_password(password, new["password_hash"])
    assert password not in site.client.get("/admin").text


def test_add_account_refuses_bad_or_taken_usernames(site: Site) -> None:
    site.login_as("boss", role=auth.SUPER_USER)
    r = site.post("/admin/accounts", username="no spaces allowed", role=auth.USER)
    assert r.status_code == 400 and len(site.db.users) == 1
    assert 'class="secret"' not in r.text
    r = site.post("/admin/accounts", username="boss", role=auth.USER)
    assert r.status_code == 400 and "already taken" in r.text
    r = site.post("/admin/accounts", username="valid.name", role="emperor")
    assert r.status_code == 400 and "valid.name" not in site.db.users


def test_reset_password(site: Site) -> None:
    site.db.add("jane")
    site.login_as("boss", role=auth.SUPER_USER)
    r = site.post("/admin/accounts/jane/reset")
    password = temp_password(r.text)
    jane = site.db.users["jane"]
    assert auth.verify_password(password, jane["password_hash"]) and jane["must_change_password"]
    assert not auth.verify_password("the-old-password", jane["password_hash"])
    assert site.post("/admin/accounts/nobody/reset").status_code == 404


def test_change_role_and_disable_someone_else(site: Site) -> None:
    site.db.add("jane")
    site.login_as("boss", role=auth.SUPER_USER)
    assert site.post("/admin/accounts/jane/role", role=auth.SUPER_USER).status_code == 200
    assert site.db.users["jane"]["role"] == auth.SUPER_USER
    assert site.post("/admin/accounts/jane/active", active="0").status_code == 200
    assert not site.db.users["jane"]["active"]
    assert site.post("/admin/accounts/jane/active", active="1").status_code == 200
    assert site.db.users["jane"]["active"]
    assert site.post("/admin/accounts/jane/role", role="emperor").status_code == 400


def test_cannot_disable_or_demote_yourself(site: Site) -> None:
    site.login_as("boss", role=auth.SUPER_USER)
    assert site.post("/admin/accounts/boss/active", active="0").status_code == 400
    assert site.post("/admin/accounts/boss/role", role=auth.USER).status_code == 400
    me = site.db.users["boss"]
    assert me["active"] and me["role"] == auth.SUPER_USER


@pytest.mark.parametrize("path,data", [
    ("/admin/accounts", {"username": "planted", "role": auth.SUPER_USER}),
    ("/admin/accounts/jane/reset", {}),
    ("/admin/accounts/jane/role", {"role": auth.SUPER_USER}),
    ("/admin/accounts/jane/active", {"active": "0"}),
])
def test_forms_from_another_site_are_refused(site: Site, path: str, data: dict[str, str]) -> None:
    site.db.add("jane")
    site.login_as("boss", role=auth.SUPER_USER)
    before = {k: dict(v) for k, v in site.db.users.items()}
    r = site.client.post(path, data=data, headers={"origin": "https://evil.example"})
    assert r.status_code == 403 and site.db.users == before


def test_change_password_from_another_site_is_refused(site: Site) -> None:
    site.login_as("jane")
    r = site.client.post(
        "/account", data={"current": "the-old-password", "new": "brand new one", "confirm": "brand new one"},
        headers={"referer": "https://evil.example/page"},
    )
    assert r.status_code == 403 and auth.verify_password("the-old-password", site.db.users["jane"]["password_hash"])


def test_same_site_form_with_a_port_is_accepted(site: Site) -> None:
    # Caddy passes the Host header on without the port.
    site.db.add("jane")
    site.login_as("boss", role=auth.SUPER_USER)
    r = site.client.post("/admin/accounts/jane/active", data={"active": "0"}, headers={"origin": "http://testserver:8000"})
    assert r.status_code == 200 and not site.db.users["jane"]["active"]


def test_names_are_escaped(site: Site) -> None:
    site.db.add("jane", full_name="<script>alert(1)</script>")
    site.login_as("boss", role=auth.SUPER_USER)
    page = site.client.get("/admin").text
    assert "<script>alert(1)</script>" not in page and "&lt;script&gt;" in page


def test_own_row_has_no_disable_or_role_buttons(site: Site) -> None:
    site.db.add("jane")
    site.login_as("boss", role=auth.SUPER_USER)
    page = site.client.get("/admin").text
    assert "/admin/accounts/jane/active" in page and "/admin/accounts/jane/role" in page
    assert "/admin/accounts/boss/active" not in page and "/admin/accounts/boss/role" not in page
    assert "/admin/accounts/boss/reset" in page


def _change(site: Site, current: str, new: str, confirm: str | None = None):
    return site.post("/account", current=current, new=new, confirm=new if confirm is None else confirm)


def test_change_password(site: Site) -> None:
    site.login_as("jane", must_change=True)
    assert "choose your own password" in site.client.get("/account").text

    assert _change(site, "wrong", "a brand new one").status_code == 400
    assert _change(site, "the-old-password", "a brand new one", "does not match").status_code == 400
    assert _change(site, "the-old-password", "short").status_code == 400
    assert _change(site, "the-old-password", "the-old-password").status_code == 400
    assert site.db.users["jane"]["must_change_password"]

    assert _change(site, "the-old-password", "a brand new one").status_code == 200
    jane = site.db.users["jane"]
    assert auth.verify_password("a brand new one", jane["password_hash"]) and not jane["must_change_password"]


def test_pages_come_before_the_catch_all(site: Site) -> None:
    app = FastAPI()

    @app.get("/{full_path:path}")
    async def chat_page(full_path: str):
        return PlainTextResponse("chat page")

    admin.register(app)
    client = TestClient(app, follow_redirects=False)
    assert client.get("/account").status_code == 303        # our page, not the chat page
    assert client.get("/admin").status_code == 303
    assert client.get("/anything-else").text == "chat page"


def test_login_required_wrapper(site: Site) -> None:
    async def inner(scope, receive, send):
        await PlainTextResponse("screenshot")(scope, receive, send)

    app = FastAPI()
    app.mount("/images", admin.LoginRequired(inner))
    client = TestClient(app)
    assert client.get("/images/a.png").status_code == 401
    site.me = "jane"
    assert client.get("/images/a.png").text == "screenshot"


def test_session_username_without_or_with_a_bad_cookie(monkeypatch) -> None:
    from starlette.requests import Request

    def request(cookie: str | None) -> Request:
        headers = [(b"cookie", cookie.encode())] if cookie else []
        return Request({"type": "http", "headers": headers})

    assert admin.session_username(request(None)) is None
    monkeypatch.setenv("CHAINLIT_AUTH_SECRET", "test-secret-test-secret-test-secret-test-secret")
    assert admin.session_username(request("access_token=not-a-real-token")) is None
