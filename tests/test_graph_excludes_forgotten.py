"""
Regression tests for #109 — knowledge-graph endpoints must exclude data
backed only by forgotten chunks.

Background: DELETE /api/chunks/{id} sets chunks.metadata->>'forgotten' = true
and drops the chunk from search. Before this fix, the four /api/graph/*
endpoints did not apply the same predicate — entities, mentions, related
entities, relationships, nodes, and edges all remained visible even after
their sole backing chunk had been forgotten.

Semantics locked (per issue design conversation):
  * mention_count reports only live mentions (not raw entity_mention count)
  * an entity with zero live mentions disappears from list_entities/visualize
  * an entity detail still 404s only if the entity id is unknown; a live
    entity with zero live mentions still returns detail with mention_count=0
  * a relationship whose backing chunk is forgotten is hidden
  * a relationship with chunk_id IS NULL (future manual/LLM tagging) is
    treated as "not backed by any chunk" and stays visible
"""

from __future__ import annotations

import pytest

from memory_vault.models.db import execute_query, fetch_one

pytestmark = pytest.mark.asyncio


# ---------------------------------------------------------------------------
# Seed helpers (mirror the shape used by tests/test_graph_api.py)
# ---------------------------------------------------------------------------


async def _seed_space(name: str) -> int:
    row = await fetch_one(
        "INSERT INTO memory_spaces (name) VALUES (%s) "
        "ON CONFLICT (name) DO UPDATE SET name = EXCLUDED.name RETURNING id",
        (name,),
    )
    return row["id"]


async def _seed_chunk(space_id: int, content: str = "seed content") -> str:
    row = await fetch_one(
        """INSERT INTO chunks (space_id, content, chunk_index)
           VALUES (%s, %s, 0) RETURNING id""",
        (space_id, content),
    )
    return str(row["id"])


async def _seed_entity(space_id: int, name: str, ent_type: str = "Person") -> str:
    row = await fetch_one(
        """INSERT INTO entities (name, type, space_id)
           VALUES (%s, %s, %s) RETURNING id""",
        (name, ent_type, space_id),
    )
    return str(row["id"])


async def _seed_mention(entity_id: str, chunk_id: str) -> None:
    await execute_query(
        """INSERT INTO entity_mentions
               (entity_id, chunk_id, start_offset, end_offset)
           VALUES (%s, %s, 0, 5)""",
        (entity_id, chunk_id),
    )


async def _seed_relationship(
    source_id: str, target_id: str, chunk_id: str | None, rel_type: str = "related_to"
) -> None:
    await execute_query(
        """INSERT INTO relationships
               (source_entity_id, target_entity_id, type, chunk_id)
           VALUES (%s, %s, %s, %s)""",
        (source_id, target_id, rel_type, chunk_id),
    )


async def _mark_forgotten(chunk_id: str) -> None:
    """Set the same metadata.forgotten=true flag DELETE /api/chunks/{id} sets."""
    await execute_query(
        """UPDATE chunks
              SET metadata = COALESCE(metadata, '{}'::jsonb)
                             || jsonb_build_object('forgotten', true)
            WHERE id = %s""",
        (chunk_id,),
    )


# ---------------------------------------------------------------------------
# /api/graph/entities
# ---------------------------------------------------------------------------


class TestListEntitiesExcludesForgotten:
    async def test_entity_with_only_forgotten_mentions_drops_out(self, client, auth_headers):
        space_id = await _seed_space("test-list-drop")
        chunk_id = await _seed_chunk(space_id)
        entity_id = await _seed_entity(space_id, "OnlyForgotten")
        await _seed_mention(entity_id, chunk_id)

        # Before forgetting: entity visible.
        r = await client.get("/api/graph/entities?space=test-list-drop", headers=auth_headers)
        names = [e["name"] for e in r.json()["entities"]]
        assert "OnlyForgotten" in names

        # After forgetting the sole backing chunk: entity drops out.
        await _mark_forgotten(chunk_id)
        r = await client.get("/api/graph/entities?space=test-list-drop", headers=auth_headers)
        names = [e["name"] for e in r.json()["entities"]]
        assert "OnlyForgotten" not in names

    async def test_mention_count_reflects_only_live_mentions(self, client, auth_headers):
        """The listed mention_count must count live mentions only, not raw
        entity_mention rows. Semantics: user-visible counts stay consistent
        with what they actually see."""
        space_id = await _seed_space("test-list-count")
        live_chunk = await _seed_chunk(space_id, "live")
        forgotten_chunk = await _seed_chunk(space_id, "forgotten")
        entity_id = await _seed_entity(space_id, "MixedMentions")
        await _seed_mention(entity_id, live_chunk)
        await _seed_mention(entity_id, forgotten_chunk)
        await _mark_forgotten(forgotten_chunk)

        r = await client.get("/api/graph/entities?space=test-list-count", headers=auth_headers)
        row = next(e for e in r.json()["entities"] if e["name"] == "MixedMentions")
        assert row["mention_count"] == 1


# ---------------------------------------------------------------------------
# /api/graph/entities/{id}
# ---------------------------------------------------------------------------


class TestGetEntityExcludesForgotten:
    async def test_forgotten_chunk_preview_never_surfaces(self, client, auth_headers):
        """The reported leak: entity detail's mentions list must not include
        chunk_preview text from a forgotten chunk."""
        space_id = await _seed_space("test-detail-preview")
        live_chunk = await _seed_chunk(space_id, "live preview text")
        forgotten_chunk = await _seed_chunk(space_id, "secret forgotten preview text")
        entity_id = await _seed_entity(space_id, "PreviewLeaker")
        await _seed_mention(entity_id, live_chunk)
        await _seed_mention(entity_id, forgotten_chunk)
        await _mark_forgotten(forgotten_chunk)

        r = await client.get(f"/api/graph/entities/{entity_id}", headers=auth_headers)
        assert r.status_code == 200
        previews = [m["chunk_preview"] for m in r.json()["mentions"]]
        assert not any("secret forgotten" in p for p in previews)
        assert any("live preview text" in p for p in previews)

    async def test_detail_mention_count_matches_live_mentions_shown(self, client, auth_headers):
        space_id = await _seed_space("test-detail-count")
        live_a = await _seed_chunk(space_id, "a")
        live_b = await _seed_chunk(space_id, "b")
        forgotten_c = await _seed_chunk(space_id, "c")
        entity_id = await _seed_entity(space_id, "CountConsistent")
        await _seed_mention(entity_id, live_a)
        await _seed_mention(entity_id, live_b)
        await _seed_mention(entity_id, forgotten_c)
        await _mark_forgotten(forgotten_c)

        r = await client.get(f"/api/graph/entities/{entity_id}", headers=auth_headers)
        body = r.json()
        assert body["mention_count"] == 2
        assert len(body["mentions"]) == 2

    async def test_related_excludes_relationships_backed_by_forgotten_chunks(
        self, client, auth_headers
    ):
        space_id = await _seed_space("test-detail-related")
        live_chunk = await _seed_chunk(space_id, "live")
        forgotten_chunk = await _seed_chunk(space_id, "forgotten")
        alice = await _seed_entity(space_id, "AliceRel")
        bob = await _seed_entity(space_id, "BobRel")
        carol = await _seed_entity(space_id, "CarolRel")
        # Alice → Bob backed by live chunk; Alice → Carol backed by forgotten chunk.
        await _seed_relationship(alice, bob, live_chunk)
        await _seed_relationship(alice, carol, forgotten_chunk)
        await _mark_forgotten(forgotten_chunk)

        r = await client.get(f"/api/graph/entities/{alice}", headers=auth_headers)
        related_names = [x["name"] for x in r.json()["related"]]
        assert "BobRel" in related_names
        assert "CarolRel" not in related_names


# ---------------------------------------------------------------------------
# /api/graph/relationships
# ---------------------------------------------------------------------------


class TestListRelationshipsExcludesForgotten:
    async def test_forgotten_backed_relationship_hidden(self, client, auth_headers):
        space_id = await _seed_space("test-rel-hide")
        live_chunk = await _seed_chunk(space_id, "live")
        forgotten_chunk = await _seed_chunk(space_id, "forgotten")
        a = await _seed_entity(space_id, "RelHideA")
        b = await _seed_entity(space_id, "RelHideB")
        c = await _seed_entity(space_id, "RelHideC")
        await _seed_relationship(a, b, live_chunk, rel_type="live_edge")
        await _seed_relationship(a, c, forgotten_chunk, rel_type="dead_edge")
        await _mark_forgotten(forgotten_chunk)

        r = await client.get(f"/api/graph/relationships?entity_id={a}", headers=auth_headers)
        types = [x["type"] for x in r.json()["relationships"]]
        assert "live_edge" in types
        assert "dead_edge" not in types
        assert r.json()["total"] == 1

    async def test_relationship_with_null_chunk_id_stays_visible(self, client, auth_headers):
        """chunk_id IS NULL means unbacked (future manual/LLM tags) — never
        hidden by the forgotten filter."""
        space_id = await _seed_space("test-rel-null")
        a = await _seed_entity(space_id, "NullChunkA")
        b = await _seed_entity(space_id, "NullChunkB")
        await _seed_relationship(a, b, chunk_id=None, rel_type="manual_tag")

        r = await client.get(f"/api/graph/relationships?entity_id={a}", headers=auth_headers)
        types = [x["type"] for x in r.json()["relationships"]]
        assert "manual_tag" in types


# ---------------------------------------------------------------------------
# /api/graph/visualize
# ---------------------------------------------------------------------------


class TestVisualizeExcludesForgotten:
    async def test_node_with_only_forgotten_mentions_drops_out(self, client, auth_headers):
        space_id = await _seed_space("test-viz-node")
        forgotten_chunk = await _seed_chunk(space_id, "forgotten")
        entity_id = await _seed_entity(space_id, "VizForgotten")
        await _seed_mention(entity_id, forgotten_chunk)
        await _mark_forgotten(forgotten_chunk)

        r = await client.get("/api/graph/visualize?space=test-viz-node", headers=auth_headers)
        names = [n["name"] for n in r.json()["nodes"]]
        assert "VizForgotten" not in names

    async def test_edge_backed_by_forgotten_chunk_hidden(self, client, auth_headers):
        space_id = await _seed_space("test-viz-edge")
        live_chunk = await _seed_chunk(space_id, "live")
        forgotten_chunk = await _seed_chunk(space_id, "forgotten")
        a = await _seed_entity(space_id, "VizEdgeA")
        b = await _seed_entity(space_id, "VizEdgeB")
        c = await _seed_entity(space_id, "VizEdgeC")
        # Every node needs a live mention so it survives the node filter and
        # can appear in the edge query.
        await _seed_mention(a, live_chunk)
        await _seed_mention(b, live_chunk)
        await _seed_mention(c, live_chunk)
        # A → B backed by live chunk (edge should survive)
        # A → C backed by forgotten chunk (edge should drop)
        await _seed_relationship(a, b, live_chunk, rel_type="live_ab")
        await _seed_relationship(a, c, forgotten_chunk, rel_type="dead_ac")
        await _mark_forgotten(forgotten_chunk)

        r = await client.get("/api/graph/visualize?space=test-viz-edge", headers=auth_headers)
        edge_types = [e["type"] for e in r.json()["edges"]]
        assert "live_ab" in edge_types
        assert "dead_ac" not in edge_types

    async def test_truncation_count_uses_live_mentions_too(self, client, auth_headers):
        """total_nodes_available (used for the truncation indicator) must count
        only nodes with ≥1 live mention, matching what the node query returns."""
        space_id = await _seed_space("test-viz-total")
        live_chunk = await _seed_chunk(space_id, "live")
        forgotten_chunk = await _seed_chunk(space_id, "forgotten")
        live_ent = await _seed_entity(space_id, "VizLive")
        dead_ent = await _seed_entity(space_id, "VizDead")
        await _seed_mention(live_ent, live_chunk)
        await _seed_mention(dead_ent, forgotten_chunk)
        await _mark_forgotten(forgotten_chunk)

        r = await client.get("/api/graph/visualize?space=test-viz-total", headers=auth_headers)
        body = r.json()
        assert body["node_count"] == 1
        assert body["truncated"] is False
