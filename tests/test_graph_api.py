"""
Integration tests for the /api/graph/* endpoints and extraction-on-ingest.

These tests hit the real FastAPI app through httpx.AsyncClient, talk to
the real test Postgres, and use the real spaCy extractor where relevant.
The aim is end-to-end correctness, not unit-level coverage (the unit
tests in test_extraction.py cover the extractor in isolation).
"""

from __future__ import annotations

import pytest

from src.models.db import execute_query, fetch_one


pytestmark = pytest.mark.asyncio


# ---------------------------------------------------------------------------
# Helpers — seed entities, mentions, relationships directly for endpoint tests
# ---------------------------------------------------------------------------


async def _seed_space(name: str) -> int:
    row = await fetch_one(
        "INSERT INTO memory_spaces (name) VALUES (%s) "
        "ON CONFLICT (name) DO UPDATE SET name = EXCLUDED.name RETURNING id",
        (name,),
    )
    return row["id"]


async def _seed_chunk(space_id: int, content: str = "seed") -> str:
    row = await fetch_one(
        """INSERT INTO chunks (space_id, content, chunk_index)
           VALUES (%s, %s, 0) RETURNING id""",
        (space_id, content),
    )
    return str(row["id"])


async def _seed_entity(space_id: int, name: str, ent_type: str) -> str:
    row = await fetch_one(
        """INSERT INTO entities (name, type, space_id)
           VALUES (%s, %s, %s) RETURNING id""",
        (name, ent_type, space_id),
    )
    return str(row["id"])


async def _seed_mention(entity_id: str, chunk_id: str, start: int = 0, end: int = 5) -> None:
    await execute_query(
        """INSERT INTO entity_mentions
               (entity_id, chunk_id, start_offset, end_offset)
           VALUES (%s, %s, %s, %s)""",
        (entity_id, chunk_id, start, end),
    )


async def _seed_relationship(
    source_id: str, target_id: str, chunk_id: str, rel_type: str = "related_to"
) -> None:
    await execute_query(
        """INSERT INTO relationships
               (source_entity_id, target_entity_id, type, chunk_id)
           VALUES (%s, %s, %s, %s)""",
        (source_id, target_id, rel_type, chunk_id),
    )


# ---------------------------------------------------------------------------
# /api/graph/entities
# ---------------------------------------------------------------------------


class TestListEntities:
    async def test_auth_required(self, client):
        r = await client.get("/api/graph/entities")
        assert r.status_code == 401

    async def test_empty_db_returns_empty_list(self, client, auth_headers):
        r = await client.get("/api/graph/entities", headers=auth_headers)
        assert r.status_code == 200
        body = r.json()
        assert body["entities"] == []
        assert body["total"] == 0

    async def test_list_populated_sorted_by_mention_count(self, client, auth_headers):
        space_id = await _seed_space("test")
        chunk_id = await _seed_chunk(space_id)
        alice = await _seed_entity(space_id, "Alice", "Person")
        bob = await _seed_entity(space_id, "Bob", "Person")
        # Alice has 3 mentions, Bob has 1.
        for _ in range(3):
            await _seed_mention(alice, chunk_id)
        await _seed_mention(bob, chunk_id)

        r = await client.get("/api/graph/entities", headers=auth_headers)
        assert r.status_code == 200
        body = r.json()
        assert body["total"] == 2
        names = [e["name"] for e in body["entities"]]
        assert names == ["Alice", "Bob"]  # Alice first (3 > 1 mentions)

    async def test_space_filter(self, client, auth_headers):
        s1 = await _seed_space("s1")
        s2 = await _seed_space("s2")
        c1 = await _seed_chunk(s1)
        c2 = await _seed_chunk(s2)
        e1 = await _seed_entity(s1, "OnlyInS1", "Person")
        e2 = await _seed_entity(s2, "OnlyInS2", "Person")
        await _seed_mention(e1, c1)
        await _seed_mention(e2, c2)

        r = await client.get("/api/graph/entities?space=s1", headers=auth_headers)
        body = r.json()
        assert [e["name"] for e in body["entities"]] == ["OnlyInS1"]

    async def test_type_filter(self, client, auth_headers):
        space_id = await _seed_space("test")
        chunk_id = await _seed_chunk(space_id)
        person = await _seed_entity(space_id, "Alice", "Person")
        tool = await _seed_entity(space_id, "pgvector", "Tool")
        await _seed_mention(person, chunk_id)
        await _seed_mention(tool, chunk_id)

        r = await client.get("/api/graph/entities?type=Tool", headers=auth_headers)
        body = r.json()
        assert [e["name"] for e in body["entities"]] == ["pgvector"]

    async def test_min_mentions_filter(self, client, auth_headers):
        space_id = await _seed_space("test")
        chunk_id = await _seed_chunk(space_id)
        popular = await _seed_entity(space_id, "Popular", "Person")
        rare = await _seed_entity(space_id, "Rare", "Person")
        for _ in range(5):
            await _seed_mention(popular, chunk_id)
        await _seed_mention(rare, chunk_id)

        r = await client.get("/api/graph/entities?min_mentions=3", headers=auth_headers)
        body = r.json()
        assert [e["name"] for e in body["entities"]] == ["Popular"]

    async def test_pagination(self, client, auth_headers):
        space_id = await _seed_space("test")
        chunk_id = await _seed_chunk(space_id)
        for i in range(5):
            ent = await _seed_entity(space_id, f"Entity{i}", "Person")
            # All same mention count so sort tie-breaker is name ASC.
            await _seed_mention(ent, chunk_id)

        r = await client.get("/api/graph/entities?limit=2&offset=2", headers=auth_headers)
        body = r.json()
        assert body["total"] == 5
        assert len(body["entities"]) == 2
        assert body["limit"] == 2
        assert body["offset"] == 2


# ---------------------------------------------------------------------------
# /api/graph/entities/{id}
# ---------------------------------------------------------------------------


class TestGetEntity:
    async def test_not_found(self, client, auth_headers):
        r = await client.get(
            "/api/graph/entities/00000000-0000-0000-0000-000000000000",
            headers=auth_headers,
        )
        assert r.status_code == 404

    async def test_returns_entity_with_mentions_and_related(self, client, auth_headers):
        space_id = await _seed_space("test")
        chunk_id = await _seed_chunk(space_id, content="Alice met Bob and Carol")
        alice = await _seed_entity(space_id, "Alice", "Person")
        bob = await _seed_entity(space_id, "Bob", "Person")
        carol = await _seed_entity(space_id, "Carol", "Person")
        await _seed_mention(alice, chunk_id, 0, 5)
        await _seed_mention(bob, chunk_id, 10, 13)
        await _seed_mention(carol, chunk_id, 18, 23)
        await _seed_relationship(alice, bob, chunk_id)
        await _seed_relationship(alice, carol, chunk_id)

        r = await client.get(f"/api/graph/entities/{alice}", headers=auth_headers)
        assert r.status_code == 200
        body = r.json()
        assert body["name"] == "Alice"
        assert body["mention_count"] == 1
        assert len(body["mentions"]) == 1
        assert body["mentions"][0]["chunk_id"] == chunk_id
        related_names = {r["name"] for r in body["related"]}
        assert related_names == {"Bob", "Carol"}

    async def test_related_aggregated_by_co_mention_count(self, client, auth_headers):
        space_id = await _seed_space("test")
        c1 = await _seed_chunk(space_id)
        c2 = await _seed_chunk(space_id)
        c3 = await _seed_chunk(space_id)
        alice = await _seed_entity(space_id, "Alice", "Person")
        bob = await _seed_entity(space_id, "Bob", "Person")
        # Alice co-mentioned with Bob in 3 different chunks.
        await _seed_relationship(alice, bob, c1)
        await _seed_relationship(alice, bob, c2)
        await _seed_relationship(alice, bob, c3)

        r = await client.get(f"/api/graph/entities/{alice}", headers=auth_headers)
        body = r.json()
        assert len(body["related"]) == 1
        assert body["related"][0]["name"] == "Bob"
        assert body["related"][0]["co_mention_count"] == 3


# ---------------------------------------------------------------------------
# /api/graph/relationships
# ---------------------------------------------------------------------------


class TestListRelationships:
    async def test_empty(self, client, auth_headers):
        r = await client.get("/api/graph/relationships", headers=auth_headers)
        assert r.status_code == 200
        body = r.json()
        assert body["relationships"] == []
        assert body["total"] == 0

    async def test_entity_id_filter_matches_source_or_target(self, client, auth_headers):
        space_id = await _seed_space("test")
        chunk_id = await _seed_chunk(space_id)
        alice = await _seed_entity(space_id, "Alice", "Person")
        bob = await _seed_entity(space_id, "Bob", "Person")
        carol = await _seed_entity(space_id, "Carol", "Person")
        await _seed_relationship(alice, bob, chunk_id)
        await _seed_relationship(bob, carol, chunk_id)  # Bob is source
        await _seed_relationship(carol, alice, chunk_id)  # Alice is target

        r = await client.get(
            f"/api/graph/relationships?entity_id={alice}", headers=auth_headers
        )
        body = r.json()
        assert body["total"] == 2  # Alice↔Bob and Carol↔Alice

    async def test_type_filter(self, client, auth_headers):
        space_id = await _seed_space("test")
        chunk_id = await _seed_chunk(space_id)
        alice = await _seed_entity(space_id, "Alice", "Person")
        bob = await _seed_entity(space_id, "Bob", "Person")
        await _seed_relationship(alice, bob, chunk_id, rel_type="related_to")
        await _seed_relationship(alice, bob, chunk_id, rel_type="works_on")

        r = await client.get(
            "/api/graph/relationships?type=works_on", headers=auth_headers
        )
        body = r.json()
        assert body["total"] == 1
        assert body["relationships"][0]["type"] == "works_on"


# ---------------------------------------------------------------------------
# /api/graph/visualize
# ---------------------------------------------------------------------------


class TestVisualize:
    async def test_empty_db_returns_empty_graph(self, client, auth_headers):
        r = await client.get("/api/graph/visualize", headers=auth_headers)
        assert r.status_code == 200
        body = r.json()
        assert body["nodes"] == []
        assert body["edges"] == []
        assert body["node_count"] == 0
        assert body["edge_count"] == 0
        assert body["truncated"] is False

    async def test_populated_with_truncation(self, client, auth_headers):
        space_id = await _seed_space("test")
        chunk_id = await _seed_chunk(space_id)
        # Create 5 entities each with 1 mention.
        ids = []
        for i in range(5):
            e = await _seed_entity(space_id, f"Entity{i}", "Person")
            await _seed_mention(e, chunk_id)
            ids.append(e)

        # max_nodes = 3 should truncate.
        r = await client.get(
            "/api/graph/visualize?max_nodes=3", headers=auth_headers
        )
        body = r.json()
        assert body["node_count"] == 3
        assert body["truncated"] is True

    async def test_max_nodes_cap_enforced(self, client, auth_headers):
        # FastAPI Query constraint is le=500; 501 must 422.
        r = await client.get(
            "/api/graph/visualize?max_nodes=501", headers=auth_headers
        )
        assert r.status_code == 422

    async def test_edges_only_between_surviving_nodes(self, client, auth_headers):
        space_id = await _seed_space("test")
        chunk_id = await _seed_chunk(space_id)
        # Popular has many mentions → will survive; Rare has 1 → will be pruned.
        popular = await _seed_entity(space_id, "Popular", "Person")
        rare = await _seed_entity(space_id, "Rare", "Person")
        also_popular = await _seed_entity(space_id, "AlsoPopular", "Person")
        for _ in range(5):
            await _seed_mention(popular, chunk_id)
            await _seed_mention(also_popular, chunk_id)
        await _seed_mention(rare, chunk_id)

        # Relationships: popular↔rare (one side pruned), popular↔alsoPopular (both survive).
        await _seed_relationship(popular, rare, chunk_id)
        await _seed_relationship(popular, also_popular, chunk_id)

        r = await client.get(
            "/api/graph/visualize?max_nodes=2", headers=auth_headers
        )
        body = r.json()
        surviving_ids = {n["id"] for n in body["nodes"]}
        # Rare's edge must have been pruned.
        for edge in body["edges"]:
            assert edge["source"] in surviving_ids
            assert edge["target"] in surviving_ids


# ---------------------------------------------------------------------------
# Extraction-on-ingest — full pipeline
# ---------------------------------------------------------------------------


class TestExtractionOnIngest:
    async def test_ingest_populates_graph_tables(self, client, auth_headers):
        # Use a text with clear named entities so spaCy picks them up.
        text = (
            "Barack Obama gave a speech in 2010. "
            "Later, Barack Obama met Hillary Clinton for dinner."
        )
        r = await client.post(
            "/api/ingest/text",
            headers=auth_headers,
            json={"text": text, "space": "default"},
        )
        assert r.status_code == 200
        body = r.json()
        assert body["stored"] is True
        chunk_id = body["chunk_id"]

        # Chunk landed.
        chunk = await fetch_one("SELECT id FROM chunks WHERE id = %s", (chunk_id,))
        assert chunk is not None

        # Entities populated.
        entities = await fetch_one(
            "SELECT COUNT(*) AS n FROM entities WHERE space_id = "
            "(SELECT id FROM memory_spaces WHERE name = 'default')"
        )
        assert entities["n"] >= 2, "Expected at least 2 entities from spaCy NER"

        # entity_mentions populated.
        mentions = await fetch_one(
            "SELECT COUNT(*) AS n FROM entity_mentions WHERE chunk_id = %s",
            (chunk_id,),
        )
        assert mentions["n"] >= 2

        # Relationships populated (2+ entities → at least one pair).
        rels = await fetch_one(
            "SELECT COUNT(*) AS n FROM relationships WHERE chunk_id = %s",
            (chunk_id,),
        )
        assert rels["n"] >= 1

    async def test_ingest_survives_extraction_failure(self, client, auth_headers, monkeypatch):
        """
        Simulate spaCy failing inside extract_entities; the graceful-degrade
        chain (exception → logged in _run_extraction → swallowed) must let
        the ingest complete successfully with the chunk committed and graph
        tables empty for that chunk.
        """
        from src.services import ingestion

        def broken_extract(text):
            raise RuntimeError("simulated spaCy failure")

        # Replace extract_entities where ingestion.py imported it.
        monkeypatch.setattr(ingestion, "extract_entities", broken_extract)

        r = await client.post(
            "/api/ingest/text",
            headers=auth_headers,
            json={"text": "Alice met Bob yesterday.", "space": "default"},
        )
        assert r.status_code == 200, f"Ingest should succeed despite extraction failure, got: {r.text}"
        body = r.json()
        assert body["stored"] is True
        chunk_id = body["chunk_id"]

        # Chunk committed.
        chunk = await fetch_one("SELECT id FROM chunks WHERE id = %s", (chunk_id,))
        assert chunk is not None, "Chunk must be committed even when extraction fails"

        # No graph data for this chunk.
        mentions = await fetch_one(
            "SELECT COUNT(*) AS n FROM entity_mentions WHERE chunk_id = %s",
            (chunk_id,),
        )
        assert mentions["n"] == 0
        rels = await fetch_one(
            "SELECT COUNT(*) AS n FROM relationships WHERE chunk_id = %s",
            (chunk_id,),
        )
        assert rels["n"] == 0
