"""
Merging two entities the extractor kept apart.

Extraction is literal and per-occurrence, so one real thing arrives as several
entities — "Alice", "Alice Smith", "A. Smith". Nothing automatic can safely
decide those are one person; a human looking at the graph can.

Four things make this more than an UPDATE, and each was measured on real rows
before any of it was written:

1. **The UNIQUE index.** `(lower(name), type, space_id)` means two entities
   that would collide cannot both exist, even for a statement. Renaming first
   and deleting after raises `UniqueViolation` — confirmed.
2. **Duplicate mentions.** A mention is (entity, chunk, offsets). If both
   entities matched at the *same* offsets in the same chunk, rewriting produces
   two identical rows. Nothing complains — there is no unique index — the
   mention count just inflates, and mention counts drive node size and the
   `min_mentions` filter.
3. **Self-relationships.** If the two are related to each other, rewriting both
   endpoints yields a row pointing an entity at itself.
4. **Cross-space merges.** Entities are per-space by design; merging across
   spaces would move data between them silently.

Points 2 and 3 are not in the original design note. They were found by doing a
naive merge on seeded rows and looking at what came out.
"""

from __future__ import annotations

import pytest

from memory_vault.models.db import execute_query, fetch_all, fetch_one
from memory_vault.services.spaces import ensure_space


async def _seed_chunk(space_id: int, content: str = "Alice and Alicia met") -> str:
    row = await fetch_one(
        "INSERT INTO chunks (space_id, content, chunk_index) VALUES (%s, %s, 0) RETURNING id",
        (space_id, content),
    )
    return str(row["id"])


async def _seed_entity(space_id: int, name: str, ent_type: str = "Person") -> str:
    row = await fetch_one(
        "INSERT INTO entities (name, type, space_id) VALUES (%s, %s, %s) RETURNING id",
        (name, ent_type, space_id),
    )
    return str(row["id"])


async def _seed_mention(entity_id: str, chunk_id: str, start: int, end: int) -> None:
    await execute_query(
        "INSERT INTO entity_mentions (entity_id, chunk_id, start_offset, end_offset) "
        "VALUES (%s, %s, %s, %s)",
        (entity_id, chunk_id, start, end),
    )


async def _seed_relationship(source: str, target: str, chunk_id: str | None = None) -> None:
    await execute_query(
        "INSERT INTO relationships (source_entity_id, target_entity_id, type, chunk_id) "
        "VALUES (%s, %s, 'related_to', %s)",
        (source, target, chunk_id),
    )


async def _merge(client, auth_headers, winner: str, loser: str):
    return await client.post(
        "/api/graph/entities/merge",
        json={"winner_id": winner, "loser_id": loser},
        headers=auth_headers,
    )


async def _mention_rows(entity_id: str) -> list[tuple[int, int, int]]:
    rows = await fetch_all(
        """SELECT start_offset, end_offset, COUNT(*) AS n
           FROM entity_mentions WHERE entity_id = %s
           GROUP BY start_offset, end_offset ORDER BY start_offset""",
        (entity_id,),
    )
    return [(int(r["start_offset"]), int(r["end_offset"]), int(r["n"])) for r in rows]


class TestTheMergeItself:
    async def test_mentions_move_to_the_winner(self, client, auth_headers):
        space_id = await ensure_space("em1")
        chunk = await _seed_chunk(space_id)
        alice = await _seed_entity(space_id, "Alice")
        alicia = await _seed_entity(space_id, "Alicia")
        await _seed_mention(alicia, chunk, 10, 16)

        r = await _merge(client, auth_headers, alice, alicia)

        assert r.status_code == 200, r.text
        assert r.json()["mentions_moved"] == 1
        assert await _mention_rows(alice) == [(10, 16, 1)]

    async def test_the_loser_is_deleted(self, client, auth_headers):
        space_id = await ensure_space("em2")
        alice = await _seed_entity(space_id, "Alice")
        alicia = await _seed_entity(space_id, "Alicia")

        await _merge(client, auth_headers, alice, alicia)

        assert await fetch_one("SELECT id FROM entities WHERE id = %s", (alicia,)) is None
        assert await fetch_one("SELECT id FROM entities WHERE id = %s", (alice,)) is not None

    async def test_relationship_endpoints_are_repointed(self, client, auth_headers):
        space_id = await ensure_space("em3")
        chunk = await _seed_chunk(space_id)
        alice = await _seed_entity(space_id, "Alice")
        alicia = await _seed_entity(space_id, "Alicia")
        bob = await _seed_entity(space_id, "Bob")
        await _seed_relationship(alicia, bob, chunk)
        await _seed_relationship(bob, alicia, chunk)

        r = await _merge(client, auth_headers, alice, alicia)

        assert r.json()["relationships_moved"] == 2
        rows = await fetch_all(
            "SELECT source_entity_id, target_entity_id FROM relationships WHERE chunk_id = %s",
            (chunk,),
        )
        endpoints = {str(x["source_entity_id"]) for x in rows} | {
            str(x["target_entity_id"]) for x in rows
        }
        assert alicia not in endpoints
        assert alice in endpoints

    async def test_the_response_names_both_entities(self, client, auth_headers):
        space_id = await ensure_space("em4")
        alice = await _seed_entity(space_id, "Alice")
        alicia = await _seed_entity(space_id, "Alicia")

        body = (await _merge(client, auth_headers, alice, alicia)).json()

        assert body["winner_name"] == "Alice"
        assert body["merged_name"] == "Alicia"
        assert "Alicia" in body["message"] and "Alice" in body["message"]


class TestTheUniqueIndexTrap:
    """
    `entities_name_type_space_idx` is UNIQUE on `(lower(name), type, space_id)`.
    Confirmed by seeding two entities and renaming one to match the other:
    `UniqueViolation ... Key (lower(name), type, space_id)=(alice, Person, 2)
    already exists.` So the loser has to be deleted in the same transaction.
    """

    async def test_two_entities_with_the_same_name_can_be_merged(self, client, auth_headers):
        """
        The case that would fail if the delete happened after the transaction:
        the graph briefly holding two rows that the index forbids.
        """
        space_id = await ensure_space("eu1")
        chunk = await _seed_chunk(space_id)
        first = await _seed_entity(space_id, "Alice", "Person")
        # Same name, different type — allowed by the index, since type is part
        # of the key. This is how two "Alice" rows legitimately coexist.
        second = await _seed_entity(space_id, "Alice", "Tool")
        await _seed_mention(second, chunk, 0, 5)

        r = await _merge(client, auth_headers, first, second)

        assert r.status_code == 200, r.text
        assert await fetch_one("SELECT id FROM entities WHERE id = %s", (second,)) is None

    async def test_the_index_still_guards_the_pair(self):
        """
        Pins the assumption the merge order depends on. If a migration drops
        this index, the reasoning in `merge_entities` stops being necessary —
        and should have to argue with a test rather than quietly rot.
        """
        row = await fetch_one(
            """SELECT indexdef FROM pg_indexes
               WHERE indexname = 'entities_name_type_space_idx'"""
        )
        assert row is not None, "the unique index the merge order depends on is gone"
        assert "UNIQUE" in row["indexdef"].upper()


class TestDuplicateMentions:
    """
    Not in the original design note. Found by running a naive merge on seeded
    rows: two mentions at the same offsets in the same chunk both survived,
    because nothing in the schema forbids it.
    """

    async def test_a_mention_at_the_same_place_is_not_duplicated(self, client, auth_headers):
        space_id = await ensure_space("ed1")
        chunk = await _seed_chunk(space_id)
        alice = await _seed_entity(space_id, "Alice")
        alicia = await _seed_entity(space_id, "Alicia")
        await _seed_mention(alice, chunk, 0, 5)
        await _seed_mention(alicia, chunk, 0, 5)

        r = await _merge(client, auth_headers, alice, alicia)

        assert r.status_code == 200, r.text
        assert await _mention_rows(alice) == [(0, 5, 1)], (
            "the same span in the same memory is one fact, not two"
        )
        assert r.json()["duplicate_mentions_dropped"] == 1

    async def test_mentions_at_different_offsets_are_both_kept(self, client, auth_headers):
        """
        The other half, and the reason this cannot just deduplicate by chunk:
        two spans in one memory are two real occurrences.
        """
        space_id = await ensure_space("ed2")
        chunk = await _seed_chunk(space_id)
        alice = await _seed_entity(space_id, "Alice")
        alicia = await _seed_entity(space_id, "Alicia")
        await _seed_mention(alice, chunk, 0, 5)
        await _seed_mention(alicia, chunk, 10, 16)

        r = await _merge(client, auth_headers, alice, alicia)

        assert await _mention_rows(alice) == [(0, 5, 1), (10, 16, 1)]
        assert r.json()["duplicate_mentions_dropped"] == 0

    async def test_the_mention_count_does_not_inflate(self, client, auth_headers):
        """
        Why this matters beyond tidiness: mention counts drive node size on the
        graph and the `min_mentions` filter. An inflated count makes a merged
        entity look more important than it is.
        """
        space_id = await ensure_space("ed3")
        chunk = await _seed_chunk(space_id)
        alice = await _seed_entity(space_id, "Alice")
        alicia = await _seed_entity(space_id, "Alicia")
        await _seed_mention(alice, chunk, 0, 5)
        await _seed_mention(alicia, chunk, 0, 5)

        await _merge(client, auth_headers, alice, alicia)

        listing = await client.get(
            "/api/graph/entities?space=ed3&min_mentions=1", headers=auth_headers
        )
        entities = {e["name"]: e for e in listing.json()["entities"]}
        assert entities["Alice"]["mention_count"] == 1, entities


class TestSelfRelationships:
    """
    Also not in the design note. A relationship *between* the two entities has
    both endpoints rewritten to the same id, producing a node with an edge to
    itself — which is not a fact about anything.
    """

    async def test_a_relationship_between_the_two_is_dropped(self, client, auth_headers):
        space_id = await ensure_space("es1")
        chunk = await _seed_chunk(space_id)
        alice = await _seed_entity(space_id, "Alice")
        alicia = await _seed_entity(space_id, "Alicia")
        await _seed_relationship(alice, alicia, chunk)

        r = await _merge(client, auth_headers, alice, alicia)

        assert r.json()["self_relationships_dropped"] == 1
        rows = await fetch_all(
            "SELECT source_entity_id, target_entity_id FROM relationships WHERE chunk_id = %s",
            (chunk,),
        )
        assert not any(str(x["source_entity_id"]) == str(x["target_entity_id"]) for x in rows), (
            "a merged entity must not end up related to itself"
        )

    async def test_it_is_dropped_in_either_direction(self, client, auth_headers):
        space_id = await ensure_space("es2")
        chunk = await _seed_chunk(space_id)
        alice = await _seed_entity(space_id, "Alice")
        alicia = await _seed_entity(space_id, "Alicia")
        await _seed_relationship(alicia, alice, chunk)  # loser -> winner

        r = await _merge(client, auth_headers, alice, alicia)

        assert r.json()["self_relationships_dropped"] == 1

    async def test_relationships_with_others_survive(self, client, auth_headers):
        """Only the pair's own relationship goes; the rest of the graph stays."""
        space_id = await ensure_space("es3")
        chunk = await _seed_chunk(space_id)
        alice = await _seed_entity(space_id, "Alice")
        alicia = await _seed_entity(space_id, "Alicia")
        bob = await _seed_entity(space_id, "Bob")
        await _seed_relationship(alice, alicia, chunk)  # dropped
        await _seed_relationship(alicia, bob, chunk)  # repointed

        r = await _merge(client, auth_headers, alice, alicia)

        assert r.json()["self_relationships_dropped"] == 1
        assert r.json()["relationships_moved"] == 1

        rows = await fetch_all(
            "SELECT source_entity_id, target_entity_id FROM relationships WHERE chunk_id = %s",
            (chunk,),
        )
        assert len(rows) == 1
        assert str(rows[0]["source_entity_id"]) == alice
        assert str(rows[0]["target_entity_id"]) == bob


class TestRefusals:
    async def test_merging_across_spaces_is_refused(self, client, auth_headers):
        """
        Entities are per-space by design. A cross-space merge would move data
        between spaces without saying so.
        """
        space_a = await ensure_space("ex1")
        space_b = await ensure_space("ex2")
        here = await _seed_entity(space_a, "Carol")
        there = await _seed_entity(space_b, "Carol")

        r = await _merge(client, auth_headers, here, there)

        assert r.status_code == 409, r.text
        assert "different spaces" in r.json()["detail"]

    async def test_nothing_moves_when_a_cross_space_merge_is_refused(self, client, auth_headers):
        space_a = await ensure_space("ex3")
        space_b = await ensure_space("ex4")
        chunk_b = await _seed_chunk(space_b)
        here = await _seed_entity(space_a, "Carol")
        there = await _seed_entity(space_b, "Carol")
        await _seed_mention(there, chunk_b, 0, 5)

        await _merge(client, auth_headers, here, there)

        assert await fetch_one("SELECT id FROM entities WHERE id = %s", (there,)) is not None
        assert await _mention_rows(there) == [(0, 5, 1)]
        assert await _mention_rows(here) == []

    async def test_merging_an_entity_into_itself_is_refused(self, client, auth_headers):
        space_id = await ensure_space("ex5")
        alice = await _seed_entity(space_id, "Alice")

        r = await _merge(client, auth_headers, alice, alice)

        assert r.status_code == 400, r.text
        assert await fetch_one("SELECT id FROM entities WHERE id = %s", (alice,)) is not None

    @pytest.mark.parametrize("which", ["winner", "loser"])
    async def test_an_unknown_entity_is_404(self, client, auth_headers, which):
        import uuid

        space_id = await ensure_space("ex6")
        real = await _seed_entity(space_id, f"Real{which}")
        ghost = str(uuid.uuid4())

        winner, loser = (ghost, real) if which == "winner" else (real, ghost)
        r = await _merge(client, auth_headers, winner, loser)

        assert r.status_code == 404, r.text

    async def test_a_malformed_id_is_rejected(self, client, auth_headers):
        r = await client.post(
            "/api/graph/entities/merge",
            json={"winner_id": "not-a-uuid", "loser_id": "also-not"},
            headers=auth_headers,
        )
        assert r.status_code == 422, r.text

    async def test_merging_requires_a_token(self, client, auth_headers):
        space_id = await ensure_space("ex7")
        alice = await _seed_entity(space_id, "Alice")
        alicia = await _seed_entity(space_id, "Alicia")

        r = await client.post(
            "/api/graph/entities/merge", json={"winner_id": alice, "loser_id": alicia}
        )

        assert r.status_code in (401, 403), r.text
        assert await fetch_one("SELECT id FROM entities WHERE id = %s", (alicia,)) is not None


class TestItIsAllOrNothing:
    async def test_a_failed_merge_leaves_everything_where_it_was(self, client, auth_headers):
        """
        The refusals happen after the rows are locked but before anything is
        written. A partial merge — mentions moved, loser still present — would
        be worse than either outcome.
        """
        space_a = await ensure_space("et1")
        space_b = await ensure_space("et2")
        chunk_a = await _seed_chunk(space_a)
        chunk_b = await _seed_chunk(space_b)
        here = await _seed_entity(space_a, "Dave")
        there = await _seed_entity(space_b, "Dave")
        await _seed_mention(here, chunk_a, 0, 4)
        await _seed_mention(there, chunk_b, 0, 4)
        await _seed_relationship(there, there, chunk_b)

        before_here = await _mention_rows(here)
        before_there = await _mention_rows(there)

        r = await _merge(client, auth_headers, here, there)
        assert r.status_code == 409

        assert await _mention_rows(here) == before_here
        assert await _mention_rows(there) == before_there
        rows = await fetch_all("SELECT id FROM relationships WHERE chunk_id = %s", (chunk_b,))
        assert len(rows) == 1, "the relationship must survive a refused merge"

    async def test_a_crash_partway_through_rolls_everything_back(self, client, auth_headers):
        """
        The merge is several statements. If it failed between moving the
        mentions and deleting the loser, a non-transactional version would
        leave the graph in a state neither before nor after — mentions on the
        winner, loser still present, and no way to tell it happened.

        Added because mutation showed the tests above pass even with the final
        delete moved outside the transaction: the refusal cases return before
        any write, so none of them exercised rollback. This injects a failure
        at the last statement instead.
        """
        import psycopg

        from memory_vault.services.graph import merge_entities

        space_id = await ensure_space("et3")
        chunk = await _seed_chunk(space_id)
        winner = await _seed_entity(space_id, "Winner")
        loser = await _seed_entity(space_id, "Loser")
        await _seed_mention(loser, chunk, 0, 5)

        class InjectedFailure(Exception):
            pass

        original = psycopg.AsyncConnection.execute

        async def failing_execute(self, sql, params=None, **kwargs):
            if isinstance(sql, str) and sql.strip().startswith("DELETE FROM entities"):
                raise InjectedFailure("crash before the loser is deleted")
            return await original(self, sql, params, **kwargs)

        psycopg.AsyncConnection.execute = failing_execute
        try:
            with pytest.raises(InjectedFailure):
                await merge_entities(winner, loser)
        finally:
            psycopg.AsyncConnection.execute = original

        assert await _mention_rows(winner) == [], "the mention move must have rolled back"
        assert await _mention_rows(loser) == [(0, 5, 1)], "the mention belongs to the loser again"
        assert await fetch_one("SELECT id FROM entities WHERE id = %s", (loser,)) is not None
