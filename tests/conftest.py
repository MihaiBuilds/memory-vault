"""
Shared pytest fixtures.

Integration tests use a dedicated `memory_vault_test` database inside the
same Postgres instance. It's created at session start, has migrations run,
and is dropped at session end so the main `memory_vault` DB is never touched.

Each test function truncates the mutable tables (`chunks`, `api_tokens`,
`query_log`) so tests start from a clean slate without paying the cost of
re-running migrations.

Environment variables are set *before* any `src.*` module is imported so
`src.config.settings` picks up the test DB.
"""

from __future__ import annotations

import os

# Must be set before importing anything from src.* — Settings is frozen
# and reads os.environ at import time.
os.environ.setdefault("DB_HOST", "db")
os.environ.setdefault("DB_PORT", "5432")
os.environ["DB_NAME"] = "memory_vault_test"
os.environ.setdefault("DB_USER", "memory_vault")
os.environ.setdefault("DB_PASSWORD", "memory_vault")
os.environ["API_AUTH_ENABLED"] = "true"
os.environ["API_RATE_LIMIT_PER_MIN"] = "100000"  # effectively unlimited for tests

import asyncio  # noqa: E402
from pathlib import Path  # noqa: E402

import psycopg  # noqa: E402
import pytest  # noqa: E402
import pytest_asyncio  # noqa: E402


ADMIN_DSN = (
    f"postgresql://{os.environ['DB_USER']}:{os.environ['DB_PASSWORD']}"
    f"@{os.environ['DB_HOST']}:{os.environ['DB_PORT']}/postgres"
)
TEST_DB_NAME = "memory_vault_test"
MIGRATIONS_DIR = Path(__file__).resolve().parent.parent / "migrations"


# ---------------------------------------------------------------------------
# Event loop — single session-wide loop so async fixtures and tests share it.
# ---------------------------------------------------------------------------


@pytest.fixture(scope="session")
def event_loop():
    loop = asyncio.new_event_loop()
    yield loop
    loop.close()


# ---------------------------------------------------------------------------
# Test database lifecycle
# ---------------------------------------------------------------------------


def _drop_and_create_db() -> None:
    """Drop and recreate the test database. Runs outside any transaction."""
    with psycopg.connect(ADMIN_DSN, autocommit=True) as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"""SELECT pg_terminate_backend(pid)
                    FROM pg_stat_activity
                    WHERE datname = %s AND pid <> pg_backend_pid()""",
                (TEST_DB_NAME,),
            )
            cur.execute(f'DROP DATABASE IF EXISTS "{TEST_DB_NAME}"')
            cur.execute(f'CREATE DATABASE "{TEST_DB_NAME}"')


def _drop_db() -> None:
    with psycopg.connect(ADMIN_DSN, autocommit=True) as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"""SELECT pg_terminate_backend(pid)
                    FROM pg_stat_activity
                    WHERE datname = %s AND pid <> pg_backend_pid()""",
                (TEST_DB_NAME,),
            )
            cur.execute(f'DROP DATABASE IF EXISTS "{TEST_DB_NAME}"')


@pytest_asyncio.fixture(scope="session", autouse=True)
async def _test_database():
    """
    Create the test DB, run migrations, open a session-wide pool, tear down
    at end of session. The pool stays open for the whole test session so
    fixtures and the app share it.
    """
    _drop_and_create_db()

    from src.models.db import init_pool, run_migrations, close_pool

    await init_pool(min_size=1, max_size=5)
    await run_migrations()

    yield

    await close_pool()
    _drop_db()


@pytest_asyncio.fixture(autouse=True)
async def _clean_tables(_test_database):
    """Truncate mutable tables before every test."""
    from src.models.db import execute_query

    await execute_query(
        "TRUNCATE chunks, api_tokens, query_log, "
        "entities, entity_mentions, relationships "
        "RESTART IDENTITY CASCADE",
        commit=True,
    )
    yield


# ---------------------------------------------------------------------------
# FastAPI app + HTTP client
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def app():
    """
    Build a fresh FastAPI app. We skip the app's lifespan so it doesn't
    open/close a pool — the session-scoped `_test_database` fixture owns
    the pool for the entire test run.
    """
    from src.api.app import create_app

    application = create_app()
    yield application


@pytest_asyncio.fixture
async def client(app):
    """httpx.AsyncClient bound directly to the ASGI app."""
    from httpx import ASGITransport, AsyncClient

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


@pytest_asyncio.fixture
async def auth_token():
    """Create a real API token and return its plaintext."""
    from src.api.deps import create_token

    return await create_token("test-suite")


@pytest.fixture
def auth_headers(auth_token):
    return {"Authorization": f"Bearer {auth_token}"}
