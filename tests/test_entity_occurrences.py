"""
Every entity occurrence gets its own graph mention.

`extract_entities` deduplicated by case-insensitive name and type and kept only
the first hit, so the writer never saw later offsets. The schema comment for
`entity_mentions` says "one row per (entity, chunk, location)" and the writer
inserts per occurrence — the extractor upstream was defeating both.

Identity dedup still applies: repeated mentions resolve to one entity node,
because the writer upserts on (lower(name), type, space_id). What changed is
that the occurrences beneath that node are no longer collapsed.
"""

from __future__ import annotations

import pytest

from memory_vault.extraction.spacy_extractor import extract_entities
from memory_vault.models.db import fetch_all, fetch_one

pytestmark = pytest.mark.asyncio


class TestExtractorKeepsOccurrences:
    async def test_repeated_person_yields_one_mention_each(self):
        """The reported example."""
        entities = extract_entities("Alice met Alice in Boston.")
        alice = [e for e in entities if e.name.lower() == "alice"]

        assert len(alice) == 2, f"expected two Alice occurrences, got {len(alice)}"
        assert alice[0].start != alice[1].start, "each occurrence needs its own offsets"

    async def test_offsets_point_at_the_real_positions(self):
        text = "Alice met Alice in Boston."
        alice = [e for e in extract_entities(text) if e.name.lower() == "alice"]

        for e in alice:
            assert text[e.start : e.end].lower() == "alice", (
                f"offsets {e.start}:{e.end} do not span the entity"
            )

    async def test_identity_is_still_deduplicated(self):
        """
        Occurrences multiply; nodes must not. Two Alices are one entity in two
        places, not two entities.
        """
        entities = extract_entities("Alice met Alice in Boston.")
        identities = {(e.name.lower(), e.type) for e in entities}
        alice_ids = {i for i in identities if i[0] == "alice"}

        assert len(alice_ids) == 1, f"Alice should be one identity, got {alice_ids}"

    async def test_distinct_entities_are_still_distinct(self):
        entities = extract_entities("Alice met Bob in Boston.")
        names = {e.name.lower() for e in entities}

        assert "alice" in names and "bob" in names

    async def test_single_occurrence_is_unchanged(self):
        """Text without repetition must extract exactly as before."""
        entities = extract_entities("Alice met Bob in Boston.")
        alice = [e for e in entities if e.name.lower() == "alice"]

        assert len(alice) == 1


class TestMentionsReachTheDatabase:
    """
    The extractor change is only useful if the extra occurrences survive the
    write path. These go through real ingestion rather than calling the writer
    directly, so a regression anywhere between the two shows up here.
    """

    async def test_repeated_entity_stores_multiple_mentions(self, client, auth_headers):
        await client.post("/api/spaces", json={"name": "occ1"}, headers=auth_headers)
        resp = await client.post(
            "/api/ingest/text",
            json={"text": "Alice met Alice in Boston.", "space": "occ1"},
            headers=auth_headers,
        )
        assert resp.status_code == 200

        row = await fetch_one(
            """SELECT count(*) AS mentions
               FROM entity_mentions em
               JOIN entities e ON e.id = em.entity_id
               JOIN memory_spaces ms ON ms.id = e.space_id
               WHERE ms.name = %s AND lower(e.name) = 'alice'""",
            ("occ1",),
        )
        assert row["mentions"] >= 2, f"expected at least two stored mentions, got {row['mentions']}"

    async def test_repeated_entity_is_still_one_node(self, client, auth_headers):
        await client.post("/api/spaces", json={"name": "occ2"}, headers=auth_headers)
        await client.post(
            "/api/ingest/text",
            json={"text": "Alice met Alice in Boston.", "space": "occ2"},
            headers=auth_headers,
        )

        rows = await fetch_all(
            """SELECT e.id FROM entities e
               JOIN memory_spaces ms ON ms.id = e.space_id
               WHERE ms.name = %s AND lower(e.name) = 'alice'""",
            ("occ2",),
        )
        assert len(rows) == 1, f"Alice should be one node, found {len(rows)}"

    async def test_mention_offsets_are_distinct_in_the_database(self, client, auth_headers):
        await client.post("/api/spaces", json={"name": "occ3"}, headers=auth_headers)
        await client.post(
            "/api/ingest/text",
            json={"text": "Alice met Alice in Boston.", "space": "occ3"},
            headers=auth_headers,
        )

        rows = await fetch_all(
            """SELECT em.start_offset, em.end_offset
               FROM entity_mentions em
               JOIN entities e ON e.id = em.entity_id
               JOIN memory_spaces ms ON ms.id = e.space_id
               WHERE ms.name = %s AND lower(e.name) = 'alice'""",
            ("occ3",),
        )
        offsets = {(r["start_offset"], r["end_offset"]) for r in rows}
        assert len(offsets) == len(rows), "each mention should have its own offsets"
