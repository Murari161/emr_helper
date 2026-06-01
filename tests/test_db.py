"""Phase 2 smoke test — verify the database plumbing works end-to-end.

This exercises app/db/pool.py and the schema applied by app/db/schema.sql.
Run inside the app container so the connection target `db:5432` resolves:

    docker compose exec app pytest tests/test_db.py -v

Why one big test and not four small ones:
asyncpg's pool binds its connections to whichever event loop created them.
pytest-asyncio (auto mode) creates a fresh event loop per test function by
default, which means each subsequent test would create a new pool — and the
pool's internal executor caches the *first* loop, causing
`RuntimeError: Event loop is closed` on test 2. Running all the checks in a
single test function keeps the entire smoke test in one event loop and one
pool, sidestepping the issue cleanly.

What the four checks verify:
  1. Pool connects + SELECT 1
       DSN, network routing, asyncpg version, pool init function exceptions.
  2. Required extensions installed (vector, pg_trgm)
       schema.sql actually ran on this DB. Without these extensions,
       vector_cosine_ops and similarity() silently fail later.
  3. audit_log round trip (INSERT + SELECT + DELETE)
       Basic table I/O, jsonb encoding, UUID handling.
  4. vector(1024) round trip
       THE critical pgvector codec check. Without register_vector on each
       connection, vectors come back as memoryview/bytes or fail to encode.
"""
from __future__ import annotations

import json
import math

from app.db.pool import close_pool, get_pool


# pytest-asyncio's auto mode (configured in pyproject.toml) turns this
# coroutine into an asyncio test — no decorator needed.
async def test_db_smoke() -> None:
    pool = await get_pool()
    try:
        # ----- 1. Pool connects + SELECT 1 ----------------------------------
        async with pool.acquire() as conn:
            assert await conn.fetchval("SELECT 1") == 1, (
                "Pool acquired a connection but SELECT 1 returned something else."
            )

        # ----- 2. Required extensions installed -----------------------------
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT extname FROM pg_extension WHERE extname IN ('vector', 'pg_trgm')"
            )
        names = {r["extname"] for r in rows}
        assert {"vector", "pg_trgm"} <= names, (
            f"Required extensions missing: have {names}, need 'vector' and 'pg_trgm'. "
            "Did docker-entrypoint-initdb.d run schema.sql on a fresh pgdata volume?"
        )

        # ----- 3. audit_log round-trip --------------------------------------
        async with pool.acquire() as conn:
            new_id = await conn.fetchval(
                """
                INSERT INTO audit_log (user_id, event_type, payload)
                VALUES ('test-smoke', 'test_event', $1::jsonb)
                RETURNING id
                """,
                '{"check": "round_trip", "phase": 2}',
            )
            try:
                row = await conn.fetchrow(
                    "SELECT user_id, event_type, payload "
                    "FROM audit_log WHERE id = $1",
                    new_id,
                )
            finally:
                await conn.execute("DELETE FROM audit_log WHERE id = $1", new_id)

        assert row is not None
        assert row["user_id"] == "test-smoke"
        assert row["event_type"] == "test_event"
        payload = (
            json.loads(row["payload"])
            if isinstance(row["payload"], str)
            else row["payload"]
        )
        assert payload == {"check": "round_trip", "phase": 2}

        # ----- 4. vector(1024) round-trip ----------------------------------
        # Distinct value at every index so a misorder would be detectable.
        expected = [i / 1024.0 for i in range(1024)]

        async with pool.acquire() as conn:
            doc_id = await conn.fetchval(
                """
                INSERT INTO documents (title, manual_version, source_path)
                VALUES ('Smoke Test Document', 'test-vector-smoke', '/tmp/test.docx')
                RETURNING id
                """
            )
            try:
                chunk_id = await conn.fetchval(
                    """
                    INSERT INTO chunks (
                        doc_id, kind, section_path, title, content,
                        manual_version, embedding
                    )
                    VALUES ($1, 'procedure', 'Test > Smoke', 'Smoke Title',
                            'Smoke content body.', 'test-vector-smoke', $2)
                    RETURNING id
                    """,
                    doc_id,
                    expected,
                )
                stored = await conn.fetchval(
                    "SELECT embedding FROM chunks WHERE id = $1",
                    chunk_id,
                )
            finally:
                # CASCADE deletes the chunk when the document goes.
                await conn.execute("DELETE FROM documents WHERE id = $1", doc_id)

        assert stored is not None, (
            "embedding came back NULL — pgvector codec is not registered."
        )
        assert len(stored) == 1024, (
            f"embedding length is {len(stored)}, expected 1024."
        )
        for i in range(1024):
            actual = float(stored[i])
            assert math.isclose(actual, expected[i], rel_tol=1e-6, abs_tol=1e-7), (
                f"vector mismatch at index {i}: got {actual}, expected {expected[i]}"
            )

    finally:
        await close_pool()
