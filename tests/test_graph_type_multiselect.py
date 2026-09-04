"""
Multi-select type filtering on the graph endpoints.

`?type=Person` filtered by one entity type and there was no way to ask for
two. Selecting "Person" and "Tool" on the graph meant two requests and no way
to see both at once.

All three graph endpoints now split a comma-separated `type`. The single-value
form is a list of one, so every existing caller keeps working through the same
code path rather than a preserved special case.

`/relationships` filters relationship type, not entity type — a different
vocabulary (`works_on`, not `Person`), which is why it is tested separately
rather than parametrized alongside the other two.
"""

from __future__ import annotations

import pytest

from memory_vault.api.routers.graph import MAX_TYPE_FILTERS, parse_type_filter
from memory_vault.models.db import execute_query, fetch_one


async def _seed_space(name: str) -> int:
    row = await fetch_one(
        "INSERT INTO memory_spaces (name) VALUES (%s) "
        "ON CONFLICT (name) DO UPDATE SET name = EXCLUDED.name RETURNING id",
        (name,),
    )
    return row["id"]


async def _seed_chunk(space_id: int) -> str:
    row = await fetch_one(
        "INSERT INTO chunks (space_id, content, chunk_index) VALUES (%s, 'seed', 0) RETURNING id",
        (space_id,),
    )
    return str(row["id"])


async def _seed_entity(space_id: int, name: str, ent_type: str) -> str:
    row = await fetch_one(
        "INSERT INTO entities (name, type, space_id) VALUES (%s, %s, %s) RETURNING id",
        (name, ent_type, space_id),
    )
    return str(row["id"])


async def _seed_mention(entity_id: str, chunk_id: str) -> None:
    await execute_query(
        "INSERT INTO entity_mentions (entity_id, chunk_id, start_offset, end_offset) "
        "VALUES (%s, %s, 0, 5)",
        (entity_id, chunk_id),
    )


async def _seed_one_of_each(space_name: str) -> dict[str, str]:
    """One live entity per type, each with a mention so it survives min_mentions."""
    space_id = await _seed_space(space_name)
    chunk_id = await _seed_chunk(space_id)

    ids = {}
    for ent_type in ("Person", "Project", "Tool", "Concept"):
        entity_id = await _seed_entity(space_id, f"{ent_type}Name", ent_type)
        await _seed_mention(entity_id, chunk_id)
        ids[ent_type] = entity_id
    return ids


class TestParser:
    """
    The parsing is shared by all three endpoints, so its edges are worth
    pinning directly rather than only through HTTP.
    """

    def test_absent_means_no_filter(self):
        assert parse_type_filter(None) is None

    @pytest.mark.parametrize("raw", ["", "   ", ",", ",,,", " , , "])
    def test_empty_means_no_filter_not_match_nothing(self, raw):
        """
        `?type=` reaches here as an empty string — the web client sends exactly
        that when the array of selected types is empty, because URLSearchParams
        keeps the key. Treating it as "match the empty type" would return
        nothing at all, which is not what an empty selection means.
        """
        assert parse_type_filter(raw) is None

    def test_single_value_is_a_list_of_one(self):
        assert parse_type_filter("Person") == ["Person"]

    def test_several_values_split(self):
        assert parse_type_filter("Person,Tool") == ["Person", "Tool"]

    def test_whitespace_around_values_is_ignored(self):
        assert parse_type_filter(" Person , Tool ") == ["Person", "Tool"]

    def test_empty_segments_are_dropped(self):
        assert parse_type_filter("Person,,Tool") == ["Person", "Tool"]

    def test_duplicates_collapse_and_order_is_kept(self):
        assert parse_type_filter("Person,Tool,Person") == ["Person", "Tool"]

    def test_a_hostile_number_of_values_is_capped(self):
        """
        Distinct values, because duplicates collapse before the cap applies —
        a probe with 60 identical values returned one item and proved nothing
        about the cap.
        """
        raw = ",".join(f"T{i}" for i in range(5_000))
        assert len(parse_type_filter(raw)) == MAX_TYPE_FILTERS


class TestEntitiesEndpoint:
    async def test_single_type_still_works(self, client, auth_headers):
        """The form every existing caller uses."""
        await _seed_one_of_each("mt1")

        r = await client.get("/api/graph/entities?space=mt1&type=Tool", headers=auth_headers)
        assert r.status_code == 200, r.text
        assert {e["type"] for e in r.json()["entities"]} == {"Tool"}

    async def test_two_types_return_both(self, client, auth_headers):
        await _seed_one_of_each("mt2")

        r = await client.get("/api/graph/entities?space=mt2&type=Person,Tool", headers=auth_headers)
        assert r.status_code == 200, r.text
        assert {e["type"] for e in r.json()["entities"]} == {"Person", "Tool"}

    async def test_the_union_is_wider_than_either_part(self, client, auth_headers):
        """
        Guards against a filter that silently matches everything: the pair must
        return more than one type alone, and still fewer than no filter at all.
        """
        await _seed_one_of_each("mt3")

        async def _count(query: str) -> int:
            r = await client.get(f"/api/graph/entities?space=mt3{query}", headers=auth_headers)
            assert r.status_code == 200, r.text
            return r.json()["total"]

        one = await _count("&type=Person")
        two = await _count("&type=Person,Tool")
        all_types = await _count("")

        assert one == 1
        assert two == 2
        assert all_types == 4
        assert one < two < all_types

    async def test_empty_value_means_all_types(self, client, auth_headers):
        """`?type=` is what the web client sends with nothing selected."""
        await _seed_one_of_each("mt4")

        r = await client.get("/api/graph/entities?space=mt4&type=", headers=auth_headers)
        assert r.status_code == 200, r.text
        assert r.json()["total"] == 4

    async def test_an_unknown_type_matches_nothing(self, client, auth_headers):
        await _seed_one_of_each("mt5")

        r = await client.get("/api/graph/entities?space=mt5&type=Nope", headers=auth_headers)
        assert r.status_code == 200, r.text
        assert r.json()["total"] == 0

    async def test_a_known_type_alongside_an_unknown_one_still_matches(self, client, auth_headers):
        await _seed_one_of_each("mt6")

        r = await client.get("/api/graph/entities?space=mt6&type=Nope,Tool", headers=auth_headers)
        assert r.status_code == 200, r.text
        assert {e["type"] for e in r.json()["entities"]} == {"Tool"}


class TestVisualizeEndpoint:
    async def test_single_type_still_works(self, client, auth_headers):
        await _seed_one_of_each("mv1")

        r = await client.get("/api/graph/visualize?space=mv1&type=Tool", headers=auth_headers)
        assert r.status_code == 200, r.text
        assert {n["type"] for n in r.json()["nodes"]} == {"Tool"}

    async def test_two_types_return_both(self, client, auth_headers):
        await _seed_one_of_each("mv2")

        r = await client.get(
            "/api/graph/visualize?space=mv2&type=Person,Concept", headers=auth_headers
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert {n["type"] for n in body["nodes"]} == {"Person", "Concept"}
        assert body["node_count"] == 2

    async def test_the_truncation_count_respects_the_filter(self, client, auth_headers):
        """
        `truncated` compares against a second COUNT query that has its own copy
        of the WHERE clause. If the filter were applied to only one of them,
        a filtered graph would claim to be truncated when it is not.
        """
        await _seed_one_of_each("mv3")

        r = await client.get(
            "/api/graph/visualize?space=mv3&type=Person,Tool", headers=auth_headers
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["node_count"] == 2
        assert body["truncated"] is False


class TestRelationshipsEndpoint:
    """Relationship types are free-form strings from the extractor, not the
    four entity types — `works_on`, not `Person`."""

    async def _seed_edges(self, space_name: str) -> None:
        space_id = await _seed_space(space_name)
        chunk_id = await _seed_chunk(space_id)
        a = await _seed_entity(space_id, "Alice", "Person")
        b = await _seed_entity(space_id, "Bob", "Person")

        for rel_type in ("works_on", "related_to", "mentions"):
            await execute_query(
                "INSERT INTO relationships (source_entity_id, target_entity_id, type, chunk_id) "
                "VALUES (%s, %s, %s, %s)",
                (a, b, rel_type, chunk_id),
            )

    async def test_single_type_still_works(self, client, auth_headers):
        await self._seed_edges("mr1")

        r = await client.get(
            "/api/graph/relationships?space=mr1&type=works_on", headers=auth_headers
        )
        assert r.status_code == 200, r.text
        assert {x["type"] for x in r.json()["relationships"]} == {"works_on"}

    async def test_two_types_return_both(self, client, auth_headers):
        await self._seed_edges("mr2")

        r = await client.get(
            "/api/graph/relationships?space=mr2&type=works_on,mentions", headers=auth_headers
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert {x["type"] for x in body["relationships"]} == {"works_on", "mentions"}
        assert body["total"] == 2


class TestValuesStayBound:
    """
    The three WHERE clauses are still composed by string interpolation, with a
    `nosec B608` note saying the templates are literal and the values bound.
    Switching to `= ANY(%s)` had to keep that true.
    """

    async def test_a_sql_payload_is_treated_as_a_type_name(self, client, auth_headers):
        await _seed_one_of_each("mi1")

        r = await client.get(
            "/api/graph/entities",
            params={"space": "mi1", "type": "Tool'; DROP TABLE entities; --"},
            headers=auth_headers,
        )
        assert r.status_code == 200, r.text
        assert r.json()["total"] == 0, "the payload should match no type, not execute"

        still_there = await fetch_one("SELECT to_regclass('public.entities') AS t")
        assert still_there["t"] is not None, "entities table must still exist"

    async def test_a_payload_among_valid_types_does_not_change_the_others(
        self, client, auth_headers
    ):
        await _seed_one_of_each("mi2")

        r = await client.get(
            "/api/graph/entities",
            params={"space": "mi2", "type": "Tool,'); DELETE FROM entities; --"},
            headers=auth_headers,
        )
        assert r.status_code == 200, r.text
        assert {e["type"] for e in r.json()["entities"]} == {"Tool"}

        remaining = await fetch_one("SELECT COUNT(*) AS n FROM entities")
        assert int(remaining["n"]) >= 4, "no rows should have been deleted"
