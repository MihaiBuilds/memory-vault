"""Moving a memory between spaces keeps search and the graph in agreement."""

import json

import pytest
import pytest_asyncio

from memory_vault.mcp import server as mcp_server
from memory_vault.models.db import execute_query, fetch_all, fetch_one
from memory_vault.services import spaces
from memory_vault.services.ingestion import ingest_text

pytestmark = pytest.mark.asyncio

SRC = "move-src"
DST = "move-dst"
_SPACE_NAMES = (SRC, DST)


@pytest_asyncio.fixture(autouse=True)
async def _spaces():
    async def remove() -> None:
        await execute_query(
            """DELETE FROM chunks WHERE space_id IN (
                   SELECT id FROM memory_spaces WHERE name = ANY(%s))""",
            (list(_SPACE_NAMES),),
            commit=True,
        )
        await execute_query(
            """DELETE FROM entities WHERE space_id IN (
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
    await spaces.ensure_space(SRC)
    await spaces.ensure_space(DST)
    yield
    await remove()


async def _space_of(chunk_id: str) -> str:
    row = await fetch_one(
        """SELECT ms.name FROM chunks c JOIN memory_spaces ms ON ms.id = c.space_id
           WHERE c.id = %s""",
        (chunk_id,),
    )
    return row["name"]


async def _entity_spaces(chunk_id: str) -> set[str]:
    rows = await fetch_all(
        """SELECT DISTINCT ms.name FROM entities e
           JOIN memory_spaces ms ON ms.id = e.space_id
           JOIN entity_mentions em ON em.entity_id = e.id
           WHERE em.chunk_id = %s""",
        (chunk_id,),
    )
    return {r["name"] for r in rows}


async def test_move_changes_the_chunks_space():
    chunk_id = await ingest_text("a memory that will be moved", space=SRC)
    assert await _space_of(chunk_id) == SRC

    result = await spaces.move_chunk(chunk_id, DST)

    assert result["moved"] is True
    assert result["from_space"] == SRC
    assert result["to_space"] == DST
    assert await _space_of(chunk_id) == DST


async def test_move_does_not_reembed_the_content():
    """The embedding is untouched — only the space changes."""
    chunk_id = await ingest_text("vectors should survive the move", space=SRC)
    before = await fetch_one("SELECT embedding FROM chunks WHERE id = %s", (chunk_id,))

    await spaces.move_chunk(chunk_id, DST)

    after = await fetch_one("SELECT embedding FROM chunks WHERE id = %s", (chunk_id,))
    assert str(before["embedding"]) == str(after["embedding"])


async def test_move_rebuilds_the_graph_in_the_target_space():
    """Entities follow the memory instead of staying behind.

    Entities are per-space while mentions hang off the chunk, so moving the
    chunk alone leaves its entities in the space it came from — the graph then
    disagrees with search about where the memory lives.
    """
    chunk_id = await ingest_text("Mihai works with Postgres in Cluj", space=SRC)
    assert await _entity_spaces(chunk_id) == {SRC}, "no entities extracted to begin with"

    await spaces.move_chunk(chunk_id, DST)

    assert await _entity_spaces(chunk_id) == {DST}


async def test_moving_into_the_same_space_is_a_no_op():
    chunk_id = await ingest_text("staying put", space=SRC)

    result = await spaces.move_chunk(chunk_id, SRC)

    assert result["moved"] is False
    assert await _space_of(chunk_id) == SRC


async def test_move_rejects_an_unknown_chunk():
    with pytest.raises(spaces.ChunkNotFound):
        await spaces.move_chunk("00000000-0000-0000-0000-000000000000", DST)


async def test_move_rejects_an_unknown_target_space():
    """The target must exist — moving never creates a space."""
    chunk_id = await ingest_text("should not move", space=SRC)

    with pytest.raises(spaces.SpaceNotFound):
        await spaces.move_chunk(chunk_id, "no-such-target")

    assert await _space_of(chunk_id) == SRC
    row = await fetch_one("SELECT 1 FROM memory_spaces WHERE name = %s", ("no-such-target",))
    assert row is None, "a failed move created the target space"


class TestMoveOverRest:
    async def test_move_endpoint_moves_the_chunk(self, client, auth_headers):
        chunk_id = await ingest_text("moved over http", space=SRC)

        resp = await client.post(
            f"/api/chunks/{chunk_id}/move",
            headers=auth_headers,
            json={"target_space": DST},
        )

        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["success"] is True
        assert body["moved"] is True
        assert body["from_space"] == SRC
        assert body["to_space"] == DST
        assert await _space_of(chunk_id) == DST

    async def test_move_endpoint_404s_for_an_unknown_chunk(self, client, auth_headers):
        resp = await client.post(
            "/api/chunks/00000000-0000-0000-0000-000000000000/move",
            headers=auth_headers,
            json={"target_space": DST},
        )
        assert resp.status_code == 404

    async def test_move_endpoint_404s_for_an_unknown_space(self, client, auth_headers):
        chunk_id = await ingest_text("stays here", space=SRC)

        resp = await client.post(
            f"/api/chunks/{chunk_id}/move",
            headers=auth_headers,
            json={"target_space": "no-such-target"},
        )

        assert resp.status_code == 404
        assert await _space_of(chunk_id) == SRC


class TestMoveOverMcp:
    async def test_move_memory_tool_moves_the_chunk(self):
        chunk_id = await ingest_text("moved over mcp", space=SRC)

        result = json.loads(await mcp_server.move_memory(chunk_id, DST))

        assert result["success"] is True
        assert result["moved"] is True
        assert await _space_of(chunk_id) == DST

    async def test_move_memory_tool_reports_an_unknown_chunk(self):
        result = json.loads(
            await mcp_server.move_memory("00000000-0000-0000-0000-000000000000", DST)
        )
        assert result["success"] is False
        assert "not found" in result["error"].lower()

    async def test_move_memory_tool_lists_spaces_for_an_unknown_target(self):
        """The unknown-space error names the real spaces, as `remember` does."""
        chunk_id = await ingest_text("stays put", space=SRC)

        result = json.loads(await mcp_server.move_memory(chunk_id, "no-such-target"))

        assert result["success"] is False
        assert "Unknown space" in result["error"]
        assert SRC in result["error"]
        assert await _space_of(chunk_id) == SRC

    async def test_move_memory_tool_does_not_create_the_target_space(self):
        """Moving never creates a space, matching `remember`'s refusal."""
        chunk_id = await ingest_text("no space creation", space=SRC)

        json.loads(await mcp_server.move_memory(chunk_id, "conjured-by-move"))

        row = await fetch_one("SELECT 1 FROM memory_spaces WHERE name = %s", ("conjured-by-move",))
        assert row is None
