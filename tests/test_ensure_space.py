"""Spaces are created on first write, with the same name rules as elsewhere."""

import asyncio
import json

import pytest
import pytest_asyncio

from memory_vault.mcp import server as mcp_server
from memory_vault.models.db import execute_query, fetch_all, fetch_one
from memory_vault.services import spaces

pytestmark = pytest.mark.asyncio

# Every space name this module creates. The shared table-cleaning fixture
# truncates chunks but leaves memory_spaces alone, so these are removed here —
# otherwise a second run would find them already present and the
# "did not exist beforehand" assertions would be meaningless.
_SPACE_NAMES = (
    "brand-new",
    "reused",
    "made-explicitly",
    "raced",
    "auto-made",
    "never-created-by-mcp",
)


@pytest_asyncio.fixture(autouse=True)
async def _clean_spaces():
    async def remove() -> None:
        # Chunks reference the space, so they go first — a test that ingested
        # into an auto-created space leaves rows behind that would otherwise
        # block the delete with a foreign-key violation.
        await execute_query(
            """DELETE FROM chunks WHERE space_id IN (
                   SELECT id FROM memory_spaces WHERE name = ANY(%s))""",
            (list(_SPACE_NAMES),),
            commit=True,
        )
        await execute_query(
            "DELETE FROM memory_spaces WHERE name = ANY(%s)",
            (list(_SPACE_NAMES),),
            commit=True,
        )

    await remove()
    yield
    await remove()


async def _space_exists(name: str) -> bool:
    row = await fetch_one("SELECT 1 FROM memory_spaces WHERE name = %s", (name,))
    return row is not None


async def test_ensure_space_creates_a_missing_space():
    assert not await _space_exists("brand-new")

    space_id = await spaces.ensure_space("brand-new")

    assert space_id > 0
    assert await _space_exists("brand-new")


async def test_ensure_space_returns_the_existing_id():
    """An existing space is reused, not duplicated."""
    first = await spaces.ensure_space("reused")
    second = await spaces.ensure_space("reused")

    assert first == second
    rows = await fetch_all("SELECT id FROM memory_spaces WHERE name = %s", ("reused",))
    assert len(rows) == 1


async def test_ensure_space_reuses_a_space_created_the_explicit_way():
    await execute_query(
        "INSERT INTO memory_spaces (name, description) VALUES (%s, %s)",
        ("made-explicitly", "created up front"),
        commit=True,
    )

    space_id = await spaces.ensure_space("made-explicitly")

    row = await fetch_one(
        "SELECT id, description FROM memory_spaces WHERE name = %s", ("made-explicitly",)
    )
    assert space_id == int(row["id"])
    assert row["description"] == "created up front", "existing space was overwritten"


async def test_concurrent_ensure_space_creates_one_space():
    """Two writers racing on the same new name agree on one space.

    The unique constraint on the name settles the race; the caller that loses
    reads back the winner's row rather than surfacing an integrity error.
    """
    results = await asyncio.gather(
        spaces.ensure_space("raced"),
        spaces.ensure_space("raced"),
        spaces.ensure_space("raced"),
    )

    assert len(set(results)) == 1, f"callers disagreed about the space id: {results}"
    rows = await fetch_all("SELECT id FROM memory_spaces WHERE name = %s", ("raced",))
    assert len(rows) == 1


@pytest.mark.parametrize(
    "name",
    [
        "Has-Capitals",
        "has_underscore",
        "-leading-hyphen",
        "has space",
        "has.dot",
        "",
        "x" * 65,
    ],
)
async def test_ensure_space_rejects_names_the_api_would_refuse(name: str):
    """Auto-creation applies the rules the explicit create endpoint applies."""
    with pytest.raises(spaces.InvalidSpaceName):
        await spaces.ensure_space(name)

    if name:
        assert not await _space_exists(name)


@pytest.mark.parametrize("name", sorted(spaces.RESERVED_SPACE_NAMES - {"default"}))
async def test_ensure_space_rejects_reserved_names(name: str):
    with pytest.raises(spaces.InvalidSpaceName):
        await spaces.ensure_space(name)

    assert not await _space_exists(name)


async def test_ensure_space_resolves_the_seeded_default_space():
    """`default` is reserved for creation but exists, so writes to it resolve."""
    space_id = await spaces.ensure_space("default")
    assert space_id > 0


async def test_sanitized_for_log_strips_line_breaks_and_control_characters():
    """Nothing that could forge a log line survives the log-sanitiser.

    Names are validated before they reach the log, so this is a second line
    of defence rather than the only one — but it is the one that holds if the
    validation and the logging ever drift apart.
    """
    assert spaces._sanitized_for_log("work") == "work"
    assert spaces._sanitized_for_log("side-projects") == "side-projects"
    assert "\n" not in spaces._sanitized_for_log("evil\nINFO fake entry")
    assert "\r" not in spaces._sanitized_for_log("evil\r\nINFO fake entry")
    assert len(spaces._sanitized_for_log("x" * 500)) <= spaces.SPACE_NAME_MAX_LENGTH

    # Only the characters a space name may hold survive — letters from other
    # scripts and uppercase are dropped rather than passed through, which a
    # str.isalnum() filter would not do.
    assert spaces._sanitized_for_log("wörk") == "wrk"
    assert spaces._sanitized_for_log("Кириллица") == ""
    assert spaces._sanitized_for_log("UPPER") == ""
    assert spaces._sanitized_for_log("tab\there") == "tabhere"

    # The property that matters for logging: nothing that could break a log
    # line or forge an entry survives, whatever goes in. The result is not
    # necessarily a *valid* space name — "-x" stays "-x" — but it is always
    # free of line breaks and control characters, and bounded in length.
    for probe in ("evil\nfake", "wörk", "a b c", "x" * 500, "-leading", "---", "\x00\x1b[31m"):
        cleaned = spaces._sanitized_for_log(probe)
        assert not any(c in cleaned for c in "\r\n\t\x00\x1b")
        assert len(cleaned) <= spaces.SPACE_NAME_MAX_LENGTH


class TestIngestAutoCreatesSpace:
    """The REST ingest surface creates the space on first write."""

    async def test_ingest_text_into_a_new_space_succeeds(self, client, auth_headers):
        assert not await _space_exists("auto-made")

        resp = await client.post(
            "/api/ingest/text",
            headers=auth_headers,
            json={"text": "first memory in a new space", "space": "auto-made"},
        )

        assert resp.status_code == 200, resp.text
        assert resp.json()["stored"] is True
        assert await _space_exists("auto-made")

    async def test_ingest_text_rejects_an_invalid_space_name(self, client, auth_headers):
        resp = await client.post(
            "/api/ingest/text",
            headers=auth_headers,
            json={"text": "should not be stored", "space": "Bad Name"},
        )

        assert resp.status_code == 400, resp.text
        assert not await _space_exists("Bad Name")

    async def test_ingest_text_rejects_a_reserved_space_name(self, client, auth_headers):
        resp = await client.post(
            "/api/ingest/text",
            headers=auth_headers,
            json={"text": "should not be stored", "space": "admin"},
        )

        assert resp.status_code == 400, resp.text
        assert "reserved" in resp.json()["detail"].lower()
        assert not await _space_exists("admin")


class TestMcpRememberStillRefusesUnknownSpaces:
    """MCP keeps rejecting unknown spaces rather than creating them.

    Auto-creation is deliberately confined to the REST surface. A space name
    reaching `remember` comes from a model reading a prompt, where a typo
    would otherwise create a plausible-looking space that quietly splits a
    user's memories in two.
    """

    async def test_remember_into_a_missing_space_does_not_create_it(self):
        raw = await mcp_server.remember("some memory", space="never-created-by-mcp")
        result = json.loads(raw)

        assert result["stored"] is False
        assert "Unknown space" in result["error"]
        assert not await _space_exists("never-created-by-mcp")
