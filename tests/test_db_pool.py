"""Connection pool resilience."""

import psycopg
import pytest
from psycopg_pool import AsyncConnectionPool

import memory_vault.models.db as db_module
from memory_vault.config import settings
from memory_vault.models.db import init_pool

pytestmark = pytest.mark.asyncio


async def test_pool_recovers_from_connection_killed_while_idle(monkeypatch):
    """A connection dropped server-side must not surface as a query error.

    Regression guard for the pool's ``check`` callback. Without it,
    psycopg_pool hands back a connection that died while idle and the next
    query raises — the common failure when the database sits across a
    network link rather than on localhost.
    """
    # A dedicated single-connection pool, so the connection we kill is
    # necessarily the one handed back on the next checkout.
    monkeypatch.setattr(db_module, "_pool", None)
    pool = await init_pool(min_size=1, max_size=1)

    try:
        async with pool.connection() as conn:
            cur = await conn.execute("SELECT pg_backend_pid() AS pid")
            pid = (await cur.fetchone())["pid"]

        # Terminate it from an independent connection, standing in for an
        # idle timeout, network blip, or administrator shutdown.
        with psycopg.connect(settings.database_url, autocommit=True) as killer:
            killer.execute("SELECT pg_terminate_backend(%s)", (pid,))

        async with pool.connection() as conn:
            cur = await conn.execute("SELECT 1 AS ok")
            assert (await cur.fetchone())["ok"] == 1
    finally:
        await pool.close()


async def test_failed_open_is_not_cached_for_later_calls(monkeypatch):
    """A pool that fails to open must not be handed to the next caller.

    Regression guard: the module caches the pool in ``_pool``. If the
    assignment happens before ``open()`` succeeds, a database that is merely
    slow to accept connections at boot poisons the cache — every later
    ``init_pool()`` returns the dead pool and the process never recovers
    without a restart.
    """
    monkeypatch.setattr(db_module, "_pool", None)

    closed: list[bool] = []

    async def failing_open(self):
        raise psycopg.OperationalError("connection refused")

    async def record_close(self):
        closed.append(True)

    monkeypatch.setattr(AsyncConnectionPool, "open", failing_open)
    monkeypatch.setattr(AsyncConnectionPool, "close", record_close)

    with pytest.raises(psycopg.OperationalError):
        await init_pool()

    assert db_module._pool is None, "failed pool was cached"
    assert closed == [True], "failed pool was not closed"

    # The database comes back: a later call must build and open a fresh pool.
    opened: list[bool] = []

    async def working_open(self):
        opened.append(True)

    async def noop_verify():
        return None

    monkeypatch.setattr(AsyncConnectionPool, "open", working_open)
    monkeypatch.setattr(db_module, "_verify_embedding_dimension", noop_verify)

    pool = await init_pool()

    assert opened == [True]
    assert db_module._pool is pool


async def test_failed_dimension_check_is_not_cached(monkeypatch):
    """The post-open verification failing must clear the cache too.

    ``_verify_embedding_dimension`` runs after ``open()`` and reaches the
    database through ``get_pool()``, so the pool has to be visible in
    ``_pool`` while it runs. If that assignment is left in place when the
    check raises, a misconfigured process keeps serving from a pool it
    already decided was unusable.
    """
    monkeypatch.setattr(db_module, "_pool", None)

    closed: list[bool] = []

    async def working_open(self):
        return None

    async def record_close(self):
        closed.append(True)

    async def failing_verify():
        raise RuntimeError("EMBEDDING_DIMENSIONS mismatch")

    monkeypatch.setattr(AsyncConnectionPool, "open", working_open)
    monkeypatch.setattr(AsyncConnectionPool, "close", record_close)
    monkeypatch.setattr(db_module, "_verify_embedding_dimension", failing_verify)

    with pytest.raises(RuntimeError, match="EMBEDDING_DIMENSIONS"):
        await init_pool()

    assert db_module._pool is None, "pool cached after dimension check failed"
    assert closed == [True], "pool was not closed after dimension check failed"
