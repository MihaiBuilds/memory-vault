"""
Filtering chunks by knowledge-graph entity.

Clicking a node on the graph had nowhere to go: you could see that an entity
existed and how often it was mentioned, but not read the memories it came
from. `/api/chunks?entity_id=...` is that link.

Two things make this less trivial than it looks, and both are asserted below.

**Duplicates.** Since #110 an entity gets one mention row per occurrence, not
one per chunk. A chunk naming Alice three times joins to three mention rows,
so a plain join returns that chunk three times and reports a total larger than
the number of chunks that exist. Measured before writing this: one seeded
chunk, naive join 2 rows, DISTINCT 1. The endpoint uses EXISTS, which asks the
question actually being asked and stops at the first hit.

**Forgotten chunks.** `live_entity_mentions` hardcodes `forgotten IS NOT TRUE`,
so joining through it would silently override `include_forgotten=true` for
entity-filtered queries only. The endpoint joins `entity_mentions` directly and
lets the existing forgotten clause apply, so the two parameters agree.
"""

from __future__ import annotations

import uuid

import pytest

from memory_vault.models.db import execute_query, fetch_all, fetch_one


async def _seed_space(client, auth_headers, name: str) -> None:
    r = await client.post("/api/spaces", json={"name": name}, headers=auth_headers)
    assert r.status_code == 201, r.text


async def _ingest(client, auth_headers, text: str, space: str) -> str:
    r = await client.post(
        "/api/ingest/text", json={"text": text, "space": space}, headers=auth_headers
    )
    assert r.status_code == 200, r.text
    return r.text


async def _entity_by_name(name: str) -> str:
    row = await fetch_one("SELECT id FROM entities WHERE name = %s LIMIT 1", (name,))
    assert row is not None, f"expected an entity named {name} to have been extracted"
    return str(row["id"])


async def _mention_count(entity_id: str) -> int:
    row = await fetch_one(
        "SELECT COUNT(*) AS n FROM entity_mentions WHERE entity_id = %s", (entity_id,)
    )
    return int(row["n"])


class TestFilteringByEntity:
    async def test_returns_only_chunks_that_mention_the_entity(self, client, auth_headers):
        await _seed_space(client, auth_headers, "ce1")
        await _ingest(client, auth_headers, "Alice shipped the release on Friday.", "ce1")
        await _ingest(client, auth_headers, "Nothing here concerns anyone in particular.", "ce1")

        alice = await _entity_by_name("Alice")
        r = await client.get(f"/api/chunks?entity_id={alice}", headers=auth_headers)

        assert r.status_code == 200, r.text
        contents = [c["content"] for c in r.json()["chunks"]]
        assert any("Alice" in c for c in contents)
        assert not any("Nothing here concerns" in c for c in contents)

    async def test_an_unknown_entity_returns_an_empty_list(self, client, auth_headers):
        """Not a 404: the query is well-formed, it simply matches nothing."""
        await _seed_space(client, auth_headers, "ce2")
        await _ingest(client, auth_headers, "Alice shipped the release.", "ce2")

        r = await client.get(f"/api/chunks?entity_id={uuid.uuid4()}", headers=auth_headers)

        assert r.status_code == 200, r.text
        assert r.json()["total"] == 0
        assert r.json()["chunks"] == []

    async def test_a_malformed_entity_id_is_rejected(self, client, auth_headers):
        r = await client.get("/api/chunks?entity_id=not-a-uuid", headers=auth_headers)
        assert r.status_code == 422, r.text

    async def test_omitting_it_lists_everything(self, client, auth_headers):
        """The default path must be untouched — every existing caller omits it."""
        await _seed_space(client, auth_headers, "ce3")
        await _ingest(client, auth_headers, "Alice shipped the release.", "ce3")
        await _ingest(client, auth_headers, "An unrelated note about nothing.", "ce3")

        r = await client.get("/api/chunks?space=ce3", headers=auth_headers)
        assert r.status_code == 200, r.text
        assert r.json()["total"] == 2


class TestRepeatedMentionsDoNotDuplicate:
    """
    The #110 trap. These are the tests that fail if EXISTS is swapped for a
    plain join, which is exactly why they are here.
    """

    async def test_a_chunk_mentioning_an_entity_repeatedly_appears_once(self, client, auth_headers):
        await _seed_space(client, auth_headers, "cd1")
        await _ingest(
            client,
            auth_headers,
            "Alice met Bob. Alice wrote the report. Alice then left.",
            "cd1",
        )

        alice = await _entity_by_name("Alice")
        mentions = await _mention_count(alice)
        assert mentions > 1, (
            f"precondition: Alice should have several mention rows, got {mentions}. "
            "Without repeated mentions this test proves nothing."
        )

        r = await client.get(f"/api/chunks?entity_id={alice}", headers=auth_headers)
        ids = [c["chunk_id"] for c in r.json()["chunks"]]

        assert len(ids) == len(set(ids)), f"the same chunk came back more than once: {ids}"

    async def test_the_total_counts_chunks_not_mentions(self, client, auth_headers):
        """
        The subtler half. A duplicate-row bug inflates `total` even when the
        page of results happens to look right, and `total` drives pagination.
        """
        await _seed_space(client, auth_headers, "cd2")
        await _ingest(
            client,
            auth_headers,
            "Alice met Bob. Alice wrote the report. Alice then left.",
            "cd2",
        )
        await _ingest(client, auth_headers, "Alice shipped the release on Friday.", "cd2")

        alice = await _entity_by_name("Alice")
        mentions = await _mention_count(alice)

        rows = await fetch_all(
            """SELECT DISTINCT c.id FROM chunks c
               JOIN entity_mentions em ON em.chunk_id = c.id
               WHERE em.entity_id = %s""",
            (alice,),
        )
        real_chunks = len(rows)

        assert mentions > real_chunks, (
            f"precondition: more mentions ({mentions}) than chunks ({real_chunks}) "
            "is what makes a naive join visibly wrong"
        )

        r = await client.get(f"/api/chunks?entity_id={alice}", headers=auth_headers)
        assert r.json()["total"] == real_chunks, (
            f"total should count chunks ({real_chunks}), not mention rows ({mentions})"
        )

    async def test_pagination_does_not_repeat_a_chunk_across_pages(self, client, auth_headers):
        await _seed_space(client, auth_headers, "cd3")
        for i in range(3):
            await _ingest(client, auth_headers, f"Alice handled task {i}. Alice signed off.", "cd3")

        alice = await _entity_by_name("Alice")

        seen: list[str] = []
        for offset in (0, 1, 2):
            r = await client.get(
                f"/api/chunks?entity_id={alice}&limit=1&offset={offset}", headers=auth_headers
            )
            seen.extend(c["chunk_id"] for c in r.json()["chunks"])

        assert len(seen) == len(set(seen)), f"a chunk appeared on more than one page: {seen}"


class TestForgottenChunksStayConsistent:
    """
    `include_forgotten` must mean the same thing with and without an entity
    filter. Joining through `live_entity_mentions` would break exactly this.
    """

    async def test_forgotten_chunks_are_hidden_by_default(self, client, auth_headers):
        await _seed_space(client, auth_headers, "cf1")
        await _ingest(client, auth_headers, "Alice shipped the release.", "cf1")
        await _ingest(client, auth_headers, "Alice also fixed the build.", "cf1")

        alice = await _entity_by_name("Alice")
        before = (await client.get(f"/api/chunks?entity_id={alice}", headers=auth_headers)).json()
        assert before["total"] == 2, before

        victim = before["chunks"][0]["chunk_id"]
        await execute_query(
            "UPDATE chunks SET metadata = jsonb_set("
            "COALESCE(metadata, '{}'::jsonb), '{forgotten}', 'true') WHERE id = %s",
            (victim,),
        )

        after = (await client.get(f"/api/chunks?entity_id={alice}", headers=auth_headers)).json()
        assert after["total"] == 1
        assert victim not in [c["chunk_id"] for c in after["chunks"]]

    async def test_include_forgotten_still_works_with_an_entity_filter(self, client, auth_headers):
        """
        The regression that a view-based implementation would introduce: the
        flag would be silently ignored, but only for entity-filtered queries.
        """
        await _seed_space(client, auth_headers, "cf2")
        await _ingest(client, auth_headers, "Alice shipped the release.", "cf2")
        await _ingest(client, auth_headers, "Alice also fixed the build.", "cf2")

        alice = await _entity_by_name("Alice")
        listed = (await client.get(f"/api/chunks?entity_id={alice}", headers=auth_headers)).json()
        victim = listed["chunks"][0]["chunk_id"]
        await execute_query(
            "UPDATE chunks SET metadata = jsonb_set("
            "COALESCE(metadata, '{}'::jsonb), '{forgotten}', 'true') WHERE id = %s",
            (victim,),
        )

        r = await client.get(
            f"/api/chunks?entity_id={alice}&include_forgotten=true", headers=auth_headers
        )
        assert r.status_code == 200, r.text
        assert r.json()["total"] == 2, "include_forgotten must not be overridden by the filter"
        assert victim in [c["chunk_id"] for c in r.json()["chunks"]]


class TestCombiningWithOtherFilters:
    async def test_entity_and_space_filters_apply_together(self, client, auth_headers):
        """Not either/or: an entity is per-space, but the same NAME can exist
        in two spaces as two separate entities."""
        await _seed_space(client, auth_headers, "cg1")
        await _seed_space(client, auth_headers, "cg2")
        await _ingest(client, auth_headers, "Alice shipped the release.", "cg1")
        await _ingest(client, auth_headers, "Alice broke the build.", "cg2")

        row = await fetch_one(
            """SELECT e.id FROM entities e JOIN memory_spaces ms ON ms.id = e.space_id
               WHERE e.name = 'Alice' AND ms.name = 'cg1'"""
        )
        assert row is not None, "expected a per-space Alice in cg1"

        r = await client.get(f"/api/chunks?entity_id={row['id']}&space=cg1", headers=auth_headers)
        assert r.status_code == 200, r.text
        contents = [c["content"] for c in r.json()["chunks"]]
        assert any("shipped the release" in c for c in contents)
        assert not any("broke the build" in c for c in contents)

    async def test_a_mismatched_space_narrows_to_nothing(self, client, auth_headers):
        await _seed_space(client, auth_headers, "cg3")
        await _seed_space(client, auth_headers, "cg4")
        await _ingest(client, auth_headers, "Alice shipped the release.", "cg3")

        row = await fetch_one(
            """SELECT e.id FROM entities e JOIN memory_spaces ms ON ms.id = e.space_id
               WHERE e.name = 'Alice' AND ms.name = 'cg3'"""
        )
        r = await client.get(f"/api/chunks?entity_id={row['id']}&space=cg4", headers=auth_headers)
        assert r.json()["total"] == 0

    @pytest.mark.parametrize("sort", ["recent", "importance"])
    async def test_both_sort_orders_work_with_the_filter(self, client, auth_headers, sort):
        await _seed_space(client, auth_headers, f"ch-{sort}")
        await _ingest(client, auth_headers, "Alice shipped the release.", f"ch-{sort}")

        alice = await _entity_by_name("Alice")
        r = await client.get(f"/api/chunks?entity_id={alice}&sort={sort}", headers=auth_headers)
        assert r.status_code == 200, r.text


class TestValuesStayBound:
    async def test_the_where_clause_still_binds_its_values(self, client, auth_headers):
        """
        The clause is added to the same f-string-composed `where_sql` carrying
        a `nosec B608` note. A UUID-typed parameter is rejected before it
        reaches SQL, which is the property worth pinning.
        """
        r = await client.get(
            "/api/chunks", params={"entity_id": "' OR 1=1 --"}, headers=auth_headers
        )
        assert r.status_code == 422, r.text

        still_there = await fetch_one("SELECT to_regclass('public.chunks') AS t")
        assert still_there["t"] is not None
