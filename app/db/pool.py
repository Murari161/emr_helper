"""Async Postgres connection pool, shared across the whole app process.

Every module that needs DB access calls `get_pool()` and acquires a connection
via `async with pool.acquire() as conn:`. There is exactly one pool per process,
created lazily on first use and reused thereafter.

Why a singleton: asyncpg pools are cheap to acquire from but expensive to
create. Chainlit spawns short-lived handlers per chat message; making each
handler create its own pool would blow out Postgres connection limits and add
~200 ms of TCP/TLS overhead per message.

The pool registers the pgvector codec on every new connection so that
`vector(1024)` columns serialize to/from Python lists of floats without manual
casting.
"""
from __future__ import annotations

import logging
from typing import Optional

import asyncpg
from pgvector.asyncpg import register_vector

from app.config import settings

log = logging.getLogger(__name__)

_pool: Optional[asyncpg.Pool] = None


async def _init_connection(conn: asyncpg.Connection) -> None:
    """Per-connection setup. Called by asyncpg for every new connection in the pool."""
    # Register pgvector's codec so we can pass/receive Python lists for vector columns.
    await register_vector(conn)


async def get_pool() -> asyncpg.Pool:
    """Return the process-wide asyncpg pool, creating it on first call.

    Subsequent calls return the same pool instance. Concurrent callers during
    the first call will both await the same coroutine — there is no race here
    because `_pool` is a module-level variable assigned only once.
    """
    global _pool
    if _pool is None:
        log.info("Creating asyncpg pool for %s", _redacted_dsn(settings.database_url))
        _pool = await asyncpg.create_pool(
            dsn=settings.database_url,
            min_size=1,
            max_size=10,
            command_timeout=30,
            init=_init_connection,
        )
        log.info("asyncpg pool ready (size 1..10)")
    return _pool


async def close_pool() -> None:
    """Tear down the pool. Call on process shutdown to drain connections cleanly."""
    global _pool
    if _pool is not None:
        log.info("Closing asyncpg pool")
        await _pool.close()
        _pool = None


def _redacted_dsn(dsn: str) -> str:
    """Strip the password from a DSN for safe logging."""
    # postgresql://user:password@host:port/db -> postgresql://user:***@host:port/db
    import re
    return re.sub(r"://([^:]+):[^@]+@", r"://\1:***@", dsn)
