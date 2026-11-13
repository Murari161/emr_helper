"""Account pages: /admin for super users, /account for everyone (stakeholder item 5).

  /admin    add an account, reset a password, change a role, disable/enable.
            A new or reset account gets a temporary password, shown once.
  /account  choose your own password (required after an account is created
            or reset, before the assistant answers questions).

Plain server-rendered pages added to Chainlit's web server (FastAPI). They use
Chainlit's own login cookie, so there is no second login. Forms only work with
that cookie, which browsers do not send from other sites (SameSite=lax), and
only from this site (Origin check). Pages are never cached, because they can
show a temporary password.

Also here: LoginRequired, which keeps the manual screenshots (/images) behind
the login now that the browser pop-up login is gone.

Facility installations without logins (LOGIN_REQUIRED=false): everyone is let
into the chat as "guest" (app/main.py), and super users log in at
/account/login instead of Chainlit's login page. A signed cookie, STAFF_COOKIE,
then tells the chat who they are.
"""
from __future__ import annotations

import html
import logging
from datetime import datetime
from typing import Any
from urllib.parse import quote, urlparse

from chainlit.auth.cookie import clear_auth_cookie, get_token_from_cookies
from chainlit.auth.jwt import create_jwt, decode_jwt
from chainlit.config import config as chainlit_config
from chainlit.user import User
from fastapi import APIRouter, FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse, Response

from app import auth
from app.config import settings
from app.db import queries

log = logging.getLogger(__name__)

router = APIRouter()
MAX_NAME = 100


# --- who is asking -------------------------------------------------------------

def session_username(request: Request) -> str | None:
    """The logged-in username from Chainlit's login cookie, or None."""
    token = get_token_from_cookies(dict(request.cookies))
    if not token:
        return None
    try:
        return decode_jwt(token).identifier
    except Exception:
        return None


async def _account(request: Request) -> dict[str, Any] | None:
    """The logged-in, active account, read fresh from the database."""
    name = session_username(request)
    if not name:
        return None
    account = await queries.get_app_user(name)
    return account if account and account["active"] else None


# --- facility installations: super-user login ---------------------------------

STAFF_COOKIE = "emr_staff"


def staff_username(raw_cookie_header: str | None) -> str | None:
    """The username in a valid staff cookie, read from a raw Cookie header."""
    for part in (raw_cookie_header or "").split(";"):
        name, _, value = part.strip().partition("=")
        if name == STAFF_COOKIE and value:
            try:
                return decode_jwt(value).identifier
            except Exception:
                return None
    return None


async def staff_account(raw_cookie_header: str | None) -> dict[str, Any] | None:
    """The active account a staff cookie belongs to, read fresh, or None."""
    name = staff_username(raw_cookie_header)
    if not name:
        return None
    account = await queries.get_app_user(name)
    return account if account and account["active"] else None


def set_staff_cookie(request: Request, response: Response, username: str) -> None:
    response.set_cookie(
        STAFF_COOKIE,
        create_jwt(User(identifier=username)),
        max_age=chainlit_config.project.user_session_timeout,
        path="/",
        httponly=True,
        samesite="lax",
        secure=request.headers.get("x-forwarded-proto") == "https" or request.url.scheme == "https",
    )


def clear_staff_cookie(response: Response) -> None:
    response.delete_cookie(STAFF_COOKIE, path="/")


def _same_origin(request: Request) -> bool:
    """A form must be posted from this site. Compares host names only: Caddy
    passes the Host header on without the port."""
    source = request.headers.get("origin") or request.headers.get("referer")
    if not source:
        return True     # some browsers omit both; the SameSite cookie still protects
    host = (request.headers.get("host") or "").split(":")[0].lower()
    return (urlparse(source).hostname or "").lower() == host


class LoginRequired:
    """ASGI wrapper: only logged-in users get through (used for /images)."""

    def __init__(self, app: Any):
        self.app = app

    async def __call__(self, scope: dict, receive: Any, send: Any) -> None:
        if scope["type"] == "http" and not session_username(Request(scope)):
            await PlainTextResponse("Please log in to the eAFYA Assistant.", status_code=401)(scope, receive, send)
            return
        await self.app(scope, receive, send)


# --- page shell ----------------------------------------------------------------

_CSS = """
:root { --green:#0f5132; --green-light:#28a745; --bg:#f6f8f7; --card:#fff; --text:#1d2b24;
        --muted:#5d6b64; --border:#d7e0db; --red:#b00000; --amber:#8a6508; }
@media (prefers-color-scheme: dark) {
  :root { --bg:#121815; --card:#1b2420; --text:#e6efe9; --muted:#9fb0a7; --border:#2e3b35;
          --red:#ff6b6b; --amber:#e6b84a; }
}
* { box-sizing:border-box; }
body { margin:0; font-family:system-ui,-apple-system,"Segoe UI",Roboto,sans-serif; background:var(--bg);
       color:var(--text); line-height:1.45; }
header { background:var(--green); color:#fff; padding:12px 16px; display:flex; gap:16px; align-items:center;
         flex-wrap:wrap; }
header strong { font-size:1.05rem; margin-right:auto; }
header a { color:#fff; text-decoration:none; opacity:.9; }
header a:hover { opacity:1; text-decoration:underline; }
main { max-width:1000px; margin:0 auto; padding:20px 16px 40px; }
h1 { font-size:1.35rem; margin:4px 0 16px; }
h2 { font-size:1.1rem; margin:28px 0 10px; }
.card { background:var(--card); border:1px solid var(--border); border-radius:10px; padding:16px; }
.msg { border-radius:8px; padding:12px 14px; margin:0 0 16px; border:1px solid var(--border); background:var(--card); }
.msg.ok { border-left:5px solid var(--green-light); }
.msg.err { border-left:5px solid var(--red); }
.secret { font-family:ui-monospace,Consolas,monospace; font-size:1.3rem; letter-spacing:.06em; padding:6px 10px;
          border:1px dashed var(--border); border-radius:6px; display:inline-block; margin:6px 0; user-select:all; }
.table-wrap { overflow-x:auto; }
table { width:100%; border-collapse:collapse; font-size:.93rem; }
th, td { text-align:left; padding:8px 6px; border-bottom:1px solid var(--border); vertical-align:middle; }
th { color:var(--muted); font-weight:600; white-space:nowrap; }
td.actions { white-space:nowrap; }
td.actions form { display:inline; }
.tag { display:inline-block; padding:1px 8px; border-radius:999px; font-size:.8rem; border:1px solid var(--border); }
.tag.super { border-color:var(--green-light); color:var(--green-light); }
.tag.off { border-color:var(--red); color:var(--red); }
.tag.wait { border-color:var(--amber); color:var(--amber); }
label { display:block; font-weight:600; margin:12px 0 4px; }
input, select { width:100%; max-width:360px; padding:8px 10px; border:1px solid var(--border); border-radius:6px;
                background:var(--card); color:var(--text); font-size:1rem; }
button { background:var(--green-light); color:#fff; border:0; border-radius:6px; padding:8px 14px; font-size:.95rem;
         cursor:pointer; margin-top:14px; }
td button { margin:0 4px 0 0; padding:4px 10px; font-size:.85rem; }
button.quiet { background:transparent; color:var(--text); border:1px solid var(--border); }
button.danger { background:transparent; color:var(--red); border:1px solid var(--red); }
.hint { color:var(--muted); font-size:.9rem; }
"""


def _e(text: Any) -> str:
    return html.escape("" if text is None else str(text))


def _page(title: str, body: str, account: dict[str, Any] | None, status: int = 200) -> HTMLResponse:
    links = ['<a href="/">← Back to the assistant</a>']
    if account and account["role"] == auth.SUPER_USER:
        links.append('<a href="/admin">Manage accounts</a>')
    if account:
        links.append('<a href="/account">Change your password</a>')
    doc = f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>{_e(title)} · eAFYA Assistant</title><link rel="icon" href="/public/favicon.png"><style>{_CSS}</style></head>
<body><header><strong>eAFYA Assistant</strong>{''.join(links)}</header>
<main><h1>{_e(title)}</h1>{body}</main></body></html>"""
    return HTMLResponse(doc, status_code=status, headers={"Cache-Control": "no-store"})


def _when(ts: datetime | None) -> str:
    return ts.strftime("%d %b %Y %H:%M") if ts else "never"


def _msg(text: str, kind: str = "ok") -> str:
    return f'<div class="msg {kind}">{text}</div>'


def _login_redirect() -> Response:
    """Chainlit's login page, or the super-user login at a facility installation."""
    return RedirectResponse("/login" if settings.login_required else "/account/login", status_code=303)


# --- /account/login: super users at a facility installation ---------------------

_LOGIN_FAILED = (
    "Sign in failed. Check your username and password. "
    "After 5 wrong tries the account is locked for 15 minutes."
)


def _staff_login_form(error: str = "") -> str:
    return f"""{_msg(_e(error), "err") if error else ""}
<div class="card"><p>For super users. Everyone else can use the assistant without logging in.</p>
<form method="post" action="/account/login">
<label for="username">Username</label>
<input id="username" name="username" required maxlength="40" autocomplete="username">
<label for="password">Password</label>
<input id="password" name="password" type="password" required autocomplete="current-password">
<button type="submit">Log in</button></form></div>"""


@router.get("/account/login")
async def staff_login_page(request: Request) -> Response:
    if settings.login_required:
        return RedirectResponse("/login", status_code=303)       # Chainlit's own login page
    return _page("Super user login", _staff_login_form(), None)


@router.post("/account/login")
async def staff_login(request: Request) -> Response:
    if settings.login_required:
        return RedirectResponse("/login", status_code=303)
    if not _same_origin(request):
        return _page("Super user login", _msg("This form must be sent from the eAFYA Assistant itself.", "err"),
                     None, 403)
    form = await request.form()
    name = auth.normalise_username(str(form.get("username") or ""))
    password = str(form.get("password") or "")
    if auth.login_guard.is_locked(name):
        log.warning("staff login refused: %r is locked after repeated wrong passwords", name)
        return _page("Super user login", _staff_login_form(_LOGIN_FAILED), None, 400)
    account = await queries.get_app_user(name)
    # A wrong username costs the same time as a wrong password.
    ok = auth.verify_password(password, account["password_hash"] if account else auth.DUMMY_HASH)
    if not (ok and account and account["active"]):
        auth.login_guard.failed(name)
        log.info("staff login failed for %r", name)
        return _page("Super user login", _staff_login_form(_LOGIN_FAILED), None, 400)
    auth.login_guard.succeeded(name)
    await queries.touch_app_user_login(name)
    log.info("staff login: %r (%s)", name, account["role"])
    response = RedirectResponse("/", status_code=303)
    set_staff_cookie(request, response, name)
    clear_auth_cookie(request, response)     # the chat signs in again, now as this user
    return response


# --- /account: choose your own password ----------------------------------------

def _password_form(account: dict[str, Any], error: str = "") -> str:
    intro = (
        _msg("Please choose your own password before you start using the assistant.", "err")
        if account["must_change_password"] else ""
    )
    return f"""{intro}{_msg(_e(error), "err") if error else ""}
<div class="card"><p>Signed in as <strong>{_e(account["username"])}</strong>
({_e(auth.role_label(account["role"]))}).</p>
<form method="post" action="/account" autocomplete="off">
<label for="current">Current password</label>
<input id="current" name="current" type="password" required autocomplete="current-password">
<label for="new">New password</label>
<input id="new" name="new" type="password" required minlength="{auth.MIN_PASSWORD_LENGTH}" autocomplete="new-password">
<label for="confirm">New password again</label>
<input id="confirm" name="confirm" type="password" required minlength="{auth.MIN_PASSWORD_LENGTH}" autocomplete="new-password">
<p class="hint">At least {auth.MIN_PASSWORD_LENGTH} characters, and not your username.</p>
<button type="submit">Change password</button></form></div>"""


@router.get("/account/me")
async def who_am_i(request: Request) -> Response:
    """Who is logged in, read fresh, so the menu at the top right knows whether
    to show "Manage accounts" (public/script.js). This only decides which links
    appear: the pages check the role themselves."""
    no_store = {"Cache-Control": "no-store"}
    account = await _account(request)
    if not account:
        if not settings.login_required and session_username(request) == auth.GUEST:
            return JSONResponse({"username": auth.GUEST, "role": auth.USER, "guest": True}, headers=no_store)
        return JSONResponse({"detail": "Not logged in"}, status_code=401, headers=no_store)
    return JSONResponse({"username": account["username"], "role": account["role"]}, headers=no_store)


@router.get("/account")
async def account_page(request: Request) -> Response:
    account = await _account(request)
    if not account:
        return _login_redirect()
    return _page("Change your password", _password_form(account), account)


@router.post("/account")
async def change_password(request: Request) -> Response:
    account = await _account(request)
    if not account:
        return _login_redirect()
    if not _same_origin(request):
        return _page("Not allowed", _msg("This form must be sent from the eAFYA Assistant itself.", "err"), account, 403)
    form = await request.form()
    current, new, confirm = (str(form.get(k) or "") for k in ("current", "new", "confirm"))
    if not auth.verify_password(current, account["password_hash"]):
        return _page("Change your password", _password_form(account, "The current password is not correct."), account, 400)
    problem = auth.password_problem(account["username"], new, confirm, current=current)
    if problem:
        return _page("Change your password", _password_form(account, problem), account, 400)
    await queries.set_app_user_password(
        account["username"], auth.hash_password(new), must_change=False, by=account["username"]
    )
    log.info("account %r changed their password", account["username"])
    account = await queries.get_app_user(account["username"])
    return _page(
        "Password changed",
        _msg('Your password has been changed. <a href="/">Back to the assistant</a>.'),
        account,
    )


# --- /admin: super users manage accounts ---------------------------------------

async def _super_user(request: Request) -> tuple[dict[str, Any] | None, Response | None]:
    """(account, None) for an active super user, else (None, response to return)."""
    account = await _account(request)
    if not account:
        return None, _login_redirect()
    if account["role"] != auth.SUPER_USER:
        return None, _page(
            "Manage accounts",
            _msg("This page is for super users. Please contact your facility's eAFYA Focal Person.", "err"),
            account, 403,
        )
    if account["must_change_password"]:
        return None, RedirectResponse("/account", status_code=303)
    return account, None


def _row(u: dict[str, Any], me: str) -> str:
    name = u["username"]
    path = f"/admin/accounts/{quote(name)}"
    tags = [f'<span class="tag{" super" if u["role"] == auth.SUPER_USER else ""}">{_e(auth.role_label(u["role"]))}</span>']
    if not u["active"]:
        tags.append('<span class="tag off">Disabled</span>')
    elif u["must_change_password"]:
        tags.append('<span class="tag wait">Must choose a password</span>')
    actions = [f'<form method="post" action="{path}/reset"><button class="quiet" type="submit">Reset password</button></form>']
    if name != me:
        to_role = auth.USER if u["role"] == auth.SUPER_USER else auth.SUPER_USER
        actions.append(
            f'<form method="post" action="{path}/role"><input type="hidden" name="role" value="{to_role}">'
            f'<button class="quiet" type="submit">Make {_e(auth.role_label(to_role).lower())}</button></form>'
        )
        if u["active"]:
            actions.append(f'<form method="post" action="{path}/active"><input type="hidden" name="active" value="0">'
                           '<button class="danger" type="submit">Disable</button></form>')
        else:
            actions.append(f'<form method="post" action="{path}/active"><input type="hidden" name="active" value="1">'
                           '<button class="quiet" type="submit">Enable</button></form>')
    you = " <span class=\"hint\">(you)</span>" if name == me else ""
    return (f"<tr><td><strong>{_e(name)}</strong>{you}</td><td>{_e(u['full_name'])}</td><td>{' '.join(tags)}</td>"
            f"<td>{_e(_when(u['last_login_at']))}</td><td class=\"actions\">{''.join(actions)}</td></tr>")


async def _admin_page(account: dict[str, Any], notice: str = "", status: int = 200,
                      form: dict[str, str] | None = None) -> HTMLResponse:
    users = await queries.list_app_users()
    form = form or {}
    rows = "".join(_row(u, account["username"]) for u in users)
    role_options = "".join(
        f'<option value="{r}"{" selected" if form.get("role") == r else ""}>{_e(auth.role_label(r))}</option>'
        for r in auth.ROLES
    )
    body = f"""{notice}
<div class="card table-wrap"><table>
<thead><tr><th>Username</th><th>Name</th><th>Role</th><th>Last login</th><th>Actions</th></tr></thead>
<tbody>{rows}</tbody></table></div>
<p class="hint">Super users can also ask about Human Resources and User &amp; Facility Management, and manage
accounts here. Accounts are disabled rather than deleted, so the question log keeps their names.</p>
<h2>Add an account</h2>
<div class="card"><form method="post" action="/admin/accounts" autocomplete="off">
<label for="username">Username</label>
<input id="username" name="username" required maxlength="40" value="{_e(form.get("username", ""))}"
 placeholder="e.g. jnamukasa">
<label for="full_name">Full name</label>
<input id="full_name" name="full_name" maxlength="{MAX_NAME}" value="{_e(form.get("full_name", ""))}"
 placeholder="e.g. Jane Namukasa">
<label for="role">Role</label><select id="role" name="role">{role_options}</select>
<p class="hint">A temporary password is shown once after you add the account. The person chooses their own
password when they first log in.</p>
<button type="submit">Add account</button></form></div>"""
    return _page("Manage accounts", body, account, status)


def _temporary_password_notice(what: str, username: str, password: str) -> str:
    return _msg(
        f"{what} <strong>{_e(username)}</strong>. Temporary password:<br>"
        f'<span class="secret">{_e(password)}</span><br>'
        "Give it to the person privately. It is shown only once; they will choose their own "
        "password when they log in."
    )


@router.get("/admin")
async def admin_page(request: Request) -> Response:
    account, refusal = await _super_user(request)
    if refusal:
        return refusal
    return await _admin_page(account)


@router.post("/admin/accounts")
async def add_account(request: Request) -> Response:
    account, refusal = await _super_user(request)
    if refusal:
        return refusal
    if not _same_origin(request):
        return await _admin_page(account, _msg("This form must be sent from the eAFYA Assistant itself.", "err"), 403)
    form = await request.form()
    username = auth.normalise_username(str(form.get("username") or ""))
    full_name = " ".join(str(form.get("full_name") or "").split())[:MAX_NAME]
    role = str(form.get("role") or auth.USER)
    entered = {"username": username, "full_name": full_name, "role": role}
    problem = auth.username_problem(username) or (None if role in auth.ROLES else "Choose a role.")
    if problem:
        return await _admin_page(account, _msg(_e(problem), "err"), 400, entered)
    password = auth.temporary_password()
    created = await queries.create_app_user(
        username=username, full_name=full_name, role=role,
        password_hash=auth.hash_password(password), created_by=account["username"],
    )
    if not created:
        return await _admin_page(account, _msg(f"The username <strong>{_e(username)}</strong> is already taken.", "err"),
                                 400, entered)
    log.info("account %r (%s) added by %r", username, role, account["username"])
    return await _admin_page(account, _temporary_password_notice("Added", username, password))


@router.post("/admin/accounts/{username}/reset")
async def reset_password(request: Request, username: str) -> Response:
    account, refusal = await _super_user(request)
    if refusal:
        return refusal
    if not _same_origin(request):
        return await _admin_page(account, _msg("This form must be sent from the eAFYA Assistant itself.", "err"), 403)
    password = auth.temporary_password()
    if not await queries.set_app_user_password(
        username, auth.hash_password(password), must_change=True, by=account["username"]
    ):
        return await _admin_page(account, _msg("That account does not exist.", "err"), 404)
    log.info("password of %r reset by %r", username, account["username"])
    return await _admin_page(account, _temporary_password_notice("New password for", username, password))


@router.post("/admin/accounts/{username}/role")
async def change_role(request: Request, username: str) -> Response:
    account, refusal = await _super_user(request)
    if refusal:
        return refusal
    if not _same_origin(request):
        return await _admin_page(account, _msg("This form must be sent from the eAFYA Assistant itself.", "err"), 403)
    role = str((await request.form()).get("role") or "")
    if role not in auth.ROLES:
        return await _admin_page(account, _msg("Choose a role.", "err"), 400)
    if username == account["username"]:
        return await _admin_page(account, _msg("You cannot change your own role.", "err"), 400)
    if not await queries.set_app_user_role(username, role, by=account["username"]):
        return await _admin_page(account, _msg("That account does not exist.", "err"), 404)
    log.info("role of %r set to %s by %r", username, role, account["username"])
    return await _admin_page(
        account, _msg(f"<strong>{_e(username)}</strong> is now a {_e(auth.role_label(role).lower())}.")
    )


@router.post("/admin/accounts/{username}/active")
async def change_active(request: Request, username: str) -> Response:
    account, refusal = await _super_user(request)
    if refusal:
        return refusal
    if not _same_origin(request):
        return await _admin_page(account, _msg("This form must be sent from the eAFYA Assistant itself.", "err"), 403)
    active = str((await request.form()).get("active") or "") == "1"
    if username == account["username"]:
        return await _admin_page(account, _msg("You cannot disable your own account.", "err"), 400)
    if not await queries.set_app_user_active(username, active, by=account["username"]):
        return await _admin_page(account, _msg("That account does not exist.", "err"), 404)
    log.info("account %r %s by %r", username, "enabled" if active else "disabled", account["username"])
    done = "enabled" if active else "disabled. They can no longer ask questions"
    return await _admin_page(account, _msg(f"<strong>{_e(username)}</strong> is {done}."))


def register(app: FastAPI) -> None:
    """Add the account pages ahead of Chainlit's catch-all route, which would
    otherwise answer GET /admin and /account with the chat page."""
    first_new = len(app.router.routes)
    app.include_router(router)
    added = app.router.routes[first_new:]
    del app.router.routes[first_new:]
    app.router.routes[0:0] = added
    log.info("Account pages ready at /admin and /account (%d routes)", len(added))
