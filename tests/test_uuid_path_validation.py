"""
Malformed UUID path parameters.

The UUID-backed routes declared their identifiers as `str`, so a value like
`not-a-valid-identifier` reached PostgreSQL, which rejected the comparison and
left the global handler to report a generic 500. Typing them as `UUID` moves
the rejection to the route boundary: FastAPI answers 422 without opening a
database connection at all.

Every route taking a UUID in its path is covered, including
`POST /chunks/{id}/move`, which shipped after the report and had the same
defect.
"""

from __future__ import annotations

import uuid

import pytest

pytestmark = pytest.mark.asyncio

# An empty segment is deliberately absent: `/api/chunks/` matches the list
# route and redirects, so it exercises routing rather than UUID validation.
MALFORMED = [
    "not-a-valid-identifier",
    "12345",
    "../etc/passwd",
    "00000000-0000-0000-0000-00000000000",  # one digit short
    "zzzzzzzz-zzzz-zzzz-zzzz-zzzzzzzzzzzz",  # right shape, not hex
]


@pytest.mark.parametrize("bad", MALFORMED)
async def test_get_chunk_rejects_malformed_uuid(client, auth_headers, bad):
    r = await client.get(f"/api/chunks/{bad}", headers=auth_headers)
    assert r.status_code != 500, f"{bad!r} must not produce a server error"
    assert r.status_code in (404, 422), f"{bad!r} gave {r.status_code}"


@pytest.mark.parametrize("bad", MALFORMED)
async def test_delete_chunk_rejects_malformed_uuid(client, auth_headers, bad):
    r = await client.delete(f"/api/chunks/{bad}", headers=auth_headers)
    assert r.status_code != 500, f"{bad!r} must not produce a server error"
    assert r.status_code in (404, 422), f"{bad!r} gave {r.status_code}"


@pytest.mark.parametrize("bad", MALFORMED)
async def test_move_chunk_rejects_malformed_uuid(client, auth_headers, bad):
    """Not in the original report — this route shipped later with the same defect."""
    r = await client.post(
        f"/api/chunks/{bad}/move",
        json={"target_space": "somewhere"},
        headers=auth_headers,
    )
    assert r.status_code != 500, f"{bad!r} must not produce a server error"
    assert r.status_code in (404, 422), f"{bad!r} gave {r.status_code}"


@pytest.mark.parametrize("bad", MALFORMED)
async def test_get_entity_rejects_malformed_uuid(client, auth_headers, bad):
    r = await client.get(f"/api/graph/entities/{bad}", headers=auth_headers)
    assert r.status_code != 500, f"{bad!r} must not produce a server error"
    assert r.status_code in (404, 422), f"{bad!r} gave {r.status_code}"


class TestWellFormedIdsStillWork:
    """
    The point is to reject malformed input, not to break the valid path. A
    well-formed UUID that matches nothing must still be a plain 404.
    """

    async def test_unknown_chunk_is_404_not_422(self, client, auth_headers):
        missing = str(uuid.uuid4())
        r = await client.get(f"/api/chunks/{missing}", headers=auth_headers)
        assert r.status_code == 404

    async def test_unknown_entity_is_404_not_422(self, client, auth_headers):
        missing = str(uuid.uuid4())
        r = await client.get(f"/api/graph/entities/{missing}", headers=auth_headers)
        assert r.status_code == 404

    async def test_real_chunk_still_fetches(self, client, auth_headers):
        """A round trip through the retyped route, to prove it still resolves."""
        await client.post("/api/spaces", json={"name": "uuid-probe"}, headers=auth_headers)
        ingest = await client.post(
            "/api/ingest/text",
            json={"text": "A chunk fetched back by its identifier.", "space": "uuid-probe"},
            headers=auth_headers,
        )
        assert ingest.status_code == 200

        listing = await client.get(
            "/api/chunks", params={"space": "uuid-probe"}, headers=auth_headers
        )
        chunk_id = listing.json()["chunks"][0]["chunk_id"]

        fetched = await client.get(f"/api/chunks/{chunk_id}", headers=auth_headers)
        assert fetched.status_code == 200
        assert fetched.json()["chunk_id"] == chunk_id
