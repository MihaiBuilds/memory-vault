"""Connection pool resilience."""

import psycopg
import pytest

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
