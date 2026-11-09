"""Manage eAFYA Assistant accounts from the command line.

Day-to-day account work happens on the /admin page (super users). This command
is for the first super user on a new installation, and for recovery if no
super user can log in.

Usage (from inside the app container):

    # Create an account; prints a one-time temporary password
    python -m scripts.users add admin --role super_user --name "System Admin"

    # New temporary password for an existing account (also re-enables it)
    python -m scripts.users reset admin

    # List accounts
    python -m scripts.users list

    # Remove an account completely (test accounts only; disable real ones on /admin)
    python -m scripts.users delete test_user

The person logs in with the temporary password and must then choose their own.
"""
from __future__ import annotations

import argparse
import asyncio
import sys

from app import auth
from app.db import queries
from app.db.pool import close_pool

BY = "command line"


async def _add(username: str, role: str, name: str) -> int:
    username = auth.normalise_username(username)
    problem = auth.username_problem(username)
    if problem:
        print(problem, file=sys.stderr)
        return 2
    password = auth.temporary_password()
    if not await queries.create_app_user(
        username=username, full_name=name, role=role, password_hash=auth.hash_password(password), created_by=BY
    ):
        print(f"The username {username!r} is already taken. Use: reset {username}", file=sys.stderr)
        return 1
    print(f"Added {username} ({auth.role_label(role)}). Temporary password: {password}")
    print("They choose their own password at first login.")
    return 0


async def _reset(username: str) -> int:
    username = auth.normalise_username(username)
    password = auth.temporary_password()
    if not await queries.set_app_user_password(username, auth.hash_password(password), must_change=True, by=BY):
        print(f"No account called {username!r}.", file=sys.stderr)
        return 1
    await queries.set_app_user_active(username, True, by=BY)
    print(f"New temporary password for {username}: {password}")
    return 0


async def _list() -> int:
    users = await queries.list_app_users()
    if not users:
        print("No accounts yet. Create the first super user with: add <username> --role super_user")
    for u in users:
        state = "active" if u["active"] else "disabled"
        if u["active"] and u["must_change_password"]:
            state += ", must choose a password"
        print(f"{u['username']:<24} {auth.role_label(u['role']):<11} {state:<32} {u['full_name']}")
    return 0


async def _delete(username: str) -> int:
    username = auth.normalise_username(username)
    if not await queries.delete_app_user(username):
        print(f"No account called {username!r}.", file=sys.stderr)
        return 1
    print(f"Deleted {username}.")
    return 0


async def _main(args: argparse.Namespace) -> int:
    try:
        if args.command == "add":
            return await _add(args.username, args.role, args.name)
        if args.command == "reset":
            return await _reset(args.username)
        if args.command == "delete":
            return await _delete(args.username)
        return await _list()
    finally:
        await close_pool()


def main() -> None:
    parser = argparse.ArgumentParser(prog="python -m scripts.users", description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)
    add = sub.add_parser("add", help="create an account and print a temporary password")
    add.add_argument("username")
    add.add_argument("--role", choices=auth.ROLES, default=auth.USER)
    add.add_argument("--name", default="", help="full name shown on the admin page")
    reset = sub.add_parser("reset", help="print a new temporary password and re-enable the account")
    reset.add_argument("username")
    sub.add_parser("list", help="list accounts")
    delete = sub.add_parser("delete", help="remove an account completely (test accounts only)")
    delete.add_argument("username")
    sys.exit(asyncio.run(_main(parser.parse_args())))


if __name__ == "__main__":
    main()
