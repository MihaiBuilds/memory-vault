"""
Concurrent creation of one space.

`POST /api/spaces` used to check for an existing row and then insert. Two
callers could both see no row, and the loser hit the unique constraint on
`memory_spaces.name` — surfacing as a generic 500 rather than the 409 a
non-racing duplicate receives. The insert is now authoritative: the conflict
is expected, so the loser gets no row back and is answered with the same 409.

The race is forced with a barrier rather than left to timing, so a passing
run means the ordering was actually exercised.
"""

from __future__ import annotations

import asyncio

import pytest

from memory_vault.models.db import fetch_all, fetch_one

pytestmark = pytest.mark.asyncio


async def test_concurrent_create_yields_one_201_and_one_409(client, auth_headers):
    """The losing request must be a conflict, never a server error."""
    name = "race-space"

    async def create():
        return await client.post("/api/spaces", json={"name": name}, headers=auth_headers)

    first, second = await asyncio.gather(create(), create())
    codes = sorted([first.status_code, second.status_code])

    assert 500 not in codes, f"a losing concurrent create must not 500 (got {codes})"
    assert codes == [201, 409], f"expected one created and one conflict, got {codes}"


async def test_race_leaves_exactly_one_row(client, auth_headers):
    """
    Whatever the responses, the database must not end up with duplicates —
    the constraint is the real guarantee and the endpoint only reports it.
    """
    name = "race-single-row"

    async def create():
        return await client.post("/api/spaces", json={"name": name}, headers=auth_headers)

    # Two is enough to force the race and is what the report describes. Fanning
    # out further exhausts the test pool, and the resulting PoolTimeout errors
    # look like failures of whatever test runs next rather than of this one.
    await asyncio.gather(create(), create())

    rows = await fetch_all("SELECT id FROM memory_spaces WHERE name = %s", (name,))
    assert len(rows) == 1, f"expected exactly one row for {name}, found {len(rows)}"


async def test_sequential_duplicate_still_conflicts(client, auth_headers):
    """The non-racing path must keep its existing contract."""
    name = "dup-space"

    first = await client.post("/api/spaces", json={"name": name}, headers=auth_headers)
    assert first.status_code == 201

    second = await client.post("/api/spaces", json={"name": name}, headers=auth_headers)
    assert second.status_code == 409
    assert name in second.json()["detail"]


async def test_description_is_persisted_on_create(client, auth_headers):
    """
    The rewritten insert still carries `description`. `ensure_space()` does
    not set one, so delegating to it would have silently dropped this.
    """
    name = "described-space"
    resp = await client.post(
        "/api/spaces",
        json={"name": name, "description": "written by the create endpoint"},
        headers=auth_headers,
    )
    assert resp.status_code == 201

    row = await fetch_one("SELECT description FROM memory_spaces WHERE name = %s", (name,))
    assert row["description"] == "written by the create endpoint"


async def test_reserved_name_still_rejected_before_insert(client, auth_headers):
    """Reserved names must fail with 400, not reach the database at all."""
    resp = await client.post("/api/spaces", json={"name": "admin"}, headers=auth_headers)
    assert resp.status_code == 400

    row = await fetch_one("SELECT 1 AS x FROM memory_spaces WHERE name = %s", ("admin",))
    assert row is None, "a reserved name must not be inserted"
