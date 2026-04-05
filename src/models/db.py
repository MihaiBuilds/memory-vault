"""
Async database connection pool and query helpers for PostgreSQL + pgvector.

Uses psycopg 3 (async) with a connection pool. All queries go through
helper functions that handle cursor management and error logging.
"""

import logging
from pathlib import Path
from typing import Any

import psycopg
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

from src.config import settings

logger = logging.getLogger(__name__)

_pool: AsyncConnectionPool | None = None

MIGRATIONS_DIR = Path(__file__).resolve().parent.parent.parent / "migrations"


async def init_pool(min_size: int = 2, max_size: int = 10) -> AsyncConnectionPool:
    """Create and open the async connection pool."""
    global _pool
    if _pool is not None:
        return _pool

    _pool = AsyncConnectionPool(
        conninfo=settings.database_url,
        min_size=min_size,
        max_size=max_size,
        open=False,
        kwargs={"row_factory": dict_row, "autocommit": False},
    )
    await _pool.open()
    logger.info("Connection pool opened (min=%d, max=%d)", min_size, max_size)
    return _pool


async def get_pool() -> AsyncConnectionPool:
    """Return the active pool, initializing if needed."""
    if _pool is None:
        await init_pool()
    return _pool  # type: ignore[return-value]


async def close_pool() -> None:
    """Gracefully close the connection pool."""
    global _pool
    if _pool is not None:
        await _pool.close()
        _pool = None
        logger.info("Connection pool closed")


async def execute_query(
    sql: str,
    params: tuple | dict | None = None,
    *,
    commit: bool = True,
) -> int:
    """Execute a DML statement. Returns rows affected."""
    pool = await get_pool()
    async with pool.connection() as conn:
        try:
            cur = await conn.execute(sql, params)
            rowcount = cur.rowcount
            if commit:
                await conn.commit()
            return rowcount
        except Exception:
            await conn.rollback()
            logger.exception("execute_query failed — SQL: %s", sql)
            raise


async def fetch_one(
    sql: str,
    params: tuple | dict | None = None,
) -> dict[str, Any] | None:
    """Fetch a single row as a dict (or None)."""
    pool = await get_pool()
    async with pool.connection() as conn:
        cur = await conn.execute(sql, params)
        return await cur.fetchone()  # type: ignore[return-value]


async def fetch_all(
    sql: str,
    params: tuple | dict | None = None,
) -> list[dict[str, Any]]:
    """Fetch all rows as a list of dicts."""
    pool = await get_pool()
    async with pool.connection() as conn:
        cur = await conn.execute(sql, params)
        return await cur.fetchall()  # type: ignore[return-value]


async def health_check() -> dict[str, Any]:
    """Run a lightweight check and return pool + server status."""
    pool = await get_pool()
    try:
        async with pool.connection() as conn:
            row = await conn.execute("SELECT version(), now() AS server_time")
            result = await row.fetchone()
            return {
                "status": "healthy",
                "server_version": result["version"],  # type: ignore[index]
                "server_time": str(result["server_time"]),  # type: ignore[index]
                "pool_size": pool.get_stats()["pool_size"],
            }
    except Exception as e:
        logger.error("Health check failed: %s", e)
        return {"status": "unhealthy", "error": str(e)}


async def run_migrations() -> None:
    """Run all SQL migration files in order. Tracks applied migrations."""
    pool = await get_pool()
    async with pool.connection() as conn:
        # Create tracking table if it doesn't exist
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS _migrations (
                filename TEXT PRIMARY KEY,
                applied_at TIMESTAMPTZ DEFAULT now()
            )
        """)
        await conn.commit()

        # Get already-applied migrations
        cur = await conn.execute("SELECT filename FROM _migrations ORDER BY filename")
        applied = {row["filename"] for row in await cur.fetchall()}

        # Run pending migrations in order
        migration_files = sorted(MIGRATIONS_DIR.glob("*.sql"))
        for migration in migration_files:
            if migration.name in applied:
                continue

            logger.info("Applying migration: %s", migration.name)
            sql = migration.read_text()
            await conn.execute(sql)
            await conn.execute(
                "INSERT INTO _migrations (filename) VALUES (%s)",
                (migration.name,),
            )
            await conn.commit()
            logger.info("Migration applied: %s", migration.name)
