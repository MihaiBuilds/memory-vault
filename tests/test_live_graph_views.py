"""The live-graph views hide forgotten memories without hiding anything else."""

import uuid

import pytest
import pytest_asyncio

from memory_vault.models.db import execute_query, fetch_all, fetch_one
from memory_vault.services import spaces
from memory_vault.services.ingestion import ingest_text

pytestmark = pytest.mark.asyncio

SPACE = "views-space"


@pytest_asyncio.fixture(autouse=True)
async def _space():
    async def remove() -> None:
        await execute_query(
            """DELETE FROM chunks WHERE space_id IN (
                   SELECT id FROM memory_spaces WHERE name = %s)""",
            (SPACE,),
            commit=True,
        )
        await execute_query(
            """DELETE FROM entities WHERE space_id IN (
                   SELECT id FROM memory_spaces WHERE name = %s)""",
            (SPACE,),
            commit=True,
        )
        await execute_query("DELETE FROM memory_spaces WHERE name = %s", (SPACE,), commit=True)

    await remove()
    space_id = await spaces.ensure_space(SPACE)
    yield space_id
    await remove()


async def _forget(chunk_id: str) -> None:
    await execute_query(
        """UPDATE chunks
           SET importance = 0,
               metadata = COALESCE(metadata, '{}'::jsonb) || '{"forgotten": true}'::jsonb
           WHERE id = %s""",
        (chunk_id,),
        commit=True,
    )


async def _mention_rows(chunk_id: str) -> int:
    row = await fetch_one(
        "SELECT COUNT(*) AS n FROM live_entity_mentions WHERE chunk_id = %s", (chunk_id,)
    )
    return int(row["n"])


async def test_forgetting_a_chunk_hides_its_mentions_from_the_view():
    chunk_id = await ingest_text("Mihai works with Postgres in Cluj", space=SPACE)
    assert await _mention_rows(chunk_id) > 0, "no mentions extracted to begin with"

    await _forget(chunk_id)

    assert await _mention_rows(chunk_id) == 0


async def test_the_view_still_exposes_mentions_on_live_chunks():
    chunk_id = await ingest_text("Postgres powers the vault", space=SPACE)
    base = await fetch_all("SELECT id FROM entity_mentions WHERE chunk_id = %s", (chunk_id,))

    live = await fetch_all("SELECT id FROM live_entity_mentions WHERE chunk_id = %s", (chunk_id,))

    assert {r["id"] for r in live} == {r["id"] for r in base}


async def test_relationships_without_a_backing_chunk_stay_visible(_space):
    """A relationship with chunk_id IS NULL has no chunk that could be forgotten.

    These are the shape future manual or LLM-assigned links take. A view built
    on an inner join would drop the whole category, so the rule is written as
    "no forgotten chunk backs this row" rather than "a live chunk backs it".
    """
    space_id = _space
    src = str(uuid.uuid4())
    dst = str(uuid.uuid4())
    for eid, name in ((src, "alpha-entity"), (dst, "beta-entity")):
        await execute_query(
            """INSERT INTO entities (id, name, type, space_id)
               VALUES (%s, %s, 'concept', %s)""",
            (eid, name, space_id),
            commit=True,
        )
    rel_id = str(uuid.uuid4())
    await execute_query(
        """INSERT INTO relationships (id, source_entity_id, target_entity_id, type, chunk_id)
           VALUES (%s, %s, %s, 'relates_to', NULL)""",
        (rel_id, src, dst),
        commit=True,
    )

    row = await fetch_one("SELECT id FROM live_relationships WHERE id = %s", (rel_id,))

    assert row is not None, "an unbacked relationship was hidden by the view"


async def test_forgetting_a_chunk_hides_its_relationships():
    # Text chosen because extraction reliably produces relationships from it —
    # a shorter phrase yields entities but no links, which would leave this
    # test asserting nothing.
    chunk_id = await ingest_text("Sarah works on Postgres and uses Docker", space=SPACE)
    before = await fetch_all("SELECT id FROM live_relationships WHERE chunk_id = %s", (chunk_id,))
    assert before, "no relationships extracted to begin with"

    await _forget(chunk_id)

    after = await fetch_all("SELECT id FROM live_relationships WHERE chunk_id = %s", (chunk_id,))
    assert after == []

    # The rows are hidden, not deleted — forgetting is a soft delete.
    raw = await fetch_all("SELECT id FROM relationships WHERE chunk_id = %s", (chunk_id,))
    assert len(raw) == len(before)


class TestGraphEndpointsHideForgottenMemories:
    """The endpoints read through the views, so forgetting is reflected there."""

    async def test_entity_list_drops_entities_whose_only_chunk_is_forgotten(
        self, client, auth_headers
    ):
        chunk_id = await ingest_text("Docker runs on Linux at Google", space=SPACE)

        resp = await client.get(f"/api/graph/entities?space={SPACE}", headers=auth_headers)
        assert resp.status_code == 200
        before = {e["name"] for e in resp.json()["entities"]}
        assert before, "no entities extracted to begin with"

        await _forget(chunk_id)

        resp = await client.get(f"/api/graph/entities?space={SPACE}", headers=auth_headers)
        after = {e["name"] for e in resp.json()["entities"]}
        assert after == set(), f"forgotten memory still visible as entities: {after}"

    async def test_visualize_drops_nodes_for_forgotten_memories(self, client, auth_headers):
        chunk_id = await ingest_text("Docker runs on Linux at Google", space=SPACE)

        resp = await client.get(f"/api/graph/visualize?space={SPACE}", headers=auth_headers)
        assert resp.status_code == 200
        assert resp.json()["nodes"], "no nodes to begin with"

        await _forget(chunk_id)

        resp = await client.get(f"/api/graph/visualize?space={SPACE}", headers=auth_headers)
        body = resp.json()
        assert body["nodes"] == []
        assert body["edges"] == []

    async def test_relationship_list_still_answers(self, client, auth_headers):
        """The endpoint works with no filters, where the WHERE clause is empty.

        Removing the always-present forgotten predicate left `where` able to be
        empty, which would produce `WHERE ` and a syntax error if unguarded.
        """
        resp = await client.get("/api/graph/relationships", headers=auth_headers)
        assert resp.status_code == 200, resp.text
        assert "relationships" in resp.json()

    async def test_entity_detail_hides_mentions_of_forgotten_chunks(self, client, auth_headers):
        chunk_id = await ingest_text("Redis caches responses", space=SPACE)
        resp = await client.get(f"/api/graph/entities?space={SPACE}", headers=auth_headers)
        entities = resp.json()["entities"]
        assert entities, "no entities extracted to begin with"
        entity_id = entities[0]["id"]

        detail = await client.get(f"/api/graph/entities/{entity_id}", headers=auth_headers)
        assert detail.json()["mentions"], "no mentions to begin with"

        await _forget(chunk_id)

        detail = await client.get(f"/api/graph/entities/{entity_id}", headers=auth_headers)
        body = detail.json()
        assert body["mentions"] == []
        assert body["mention_count"] == 0
