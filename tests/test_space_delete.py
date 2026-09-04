"""
Deleting a memory space.

Spaces could be created but never removed, so a typo was permanent.

The endpoint refuses to delete anything but an empty space, which is a
stronger rule than it first appears. The two foreign keys pointing at a space
behave differently and neither is safe to trigger by accident:

    chunks.space_id    ON DELETE NO ACTION  -> Postgres refuses the delete
    entities.space_id  ON DELETE CASCADE    -> entities are destroyed silently,
                                               and cascade on into
                                               entity_mentions and relationships

Both rules were read off the live schema rather than the migration files, and
both are asserted here: if a later migration changes either one, the change
should have to argue with a test.
"""

from __future__ import annotations

import pytest

from memory_vault.models.db import execute_query, fetch_one
from memory_vault.services.spaces import RESERVED_SPACE_NAMES


async def _space_id(name: str) -> int:
    row = await fetch_one("SELECT id FROM memory_spaces WHERE name = %s", (name,))
    assert row is not None, f"expected space {name} to exist"
    return int(row["id"])


async def _make_space(client, auth_headers, name: str) -> int:
    r = await client.post("/api/spaces", json={"name": name}, headers=auth_headers)
    assert r.status_code == 201, r.text
    return await _space_id(name)


async def _add_entity(space_id: int, name: str = "Widget") -> str:
    row = await fetch_one(
        "INSERT INTO entities (name, type, space_id) VALUES (%s, 'Tool', %s) RETURNING id",
        (name, space_id),
    )
    return str(row["id"])


class TestDeletingAnEmptySpace:
    async def test_an_empty_space_is_deleted(self, client, auth_headers):
        await _make_space(client, auth_headers, "del1")

        r = await client.delete("/api/spaces/del1", headers=auth_headers)
        assert r.status_code == 204, r.text
        assert r.content == b"", "204 carries no body"

    async def test_it_is_gone_from_the_listing(self, client, auth_headers):
        await _make_space(client, auth_headers, "del2")
        await client.delete("/api/spaces/del2", headers=auth_headers)

        listing = await client.get("/api/spaces", headers=auth_headers)
        assert "del2" not in [s["name"] for s in listing.json()["spaces"]]

    async def test_the_row_is_really_gone(self, client, auth_headers):
        """A soft-delete would leave the name unusable while looking deleted."""
        await _make_space(client, auth_headers, "del3")
        await client.delete("/api/spaces/del3", headers=auth_headers)

        assert await fetch_one("SELECT id FROM memory_spaces WHERE name = 'del3'") is None

    async def test_the_name_can_be_used_again(self, client, auth_headers):
        """The point of deleting a typo is being able to type it correctly."""
        await _make_space(client, auth_headers, "del4")
        await client.delete("/api/spaces/del4", headers=auth_headers)

        again = await client.post("/api/spaces", json={"name": "del4"}, headers=auth_headers)
        assert again.status_code == 201, again.text

    async def test_deleting_twice_reports_it_is_gone(self, client, auth_headers):
        await _make_space(client, auth_headers, "del5")

        first = await client.delete("/api/spaces/del5", headers=auth_headers)
        second = await client.delete("/api/spaces/del5", headers=auth_headers)

        assert first.status_code == 204
        assert second.status_code == 404


class TestASpaceWithMemoriesIsRefused:
    async def test_a_space_holding_a_memory_is_refused(self, client, auth_headers):
        await _make_space(client, auth_headers, "dc1")
        ingest = await client.post(
            "/api/ingest/text",
            json={"text": "A memory that should keep its space alive.", "space": "dc1"},
            headers=auth_headers,
        )
        assert ingest.status_code == 200, ingest.text

        r = await client.delete("/api/spaces/dc1", headers=auth_headers)
        assert r.status_code == 409, r.text
        assert "not empty" in r.json()["detail"]

    async def test_the_memory_survives_the_refusal(self, client, auth_headers):
        """The refusal has to be a refusal, not a partial delete."""
        await _make_space(client, auth_headers, "dc2")
        await client.post(
            "/api/ingest/text",
            json={"text": "Still here afterwards.", "space": "dc2"},
            headers=auth_headers,
        )
        space_id = await _space_id("dc2")

        await client.delete("/api/spaces/dc2", headers=auth_headers)

        row = await fetch_one("SELECT COUNT(*) AS n FROM chunks WHERE space_id = %s", (space_id,))
        assert int(row["n"]) >= 1, "the chunk must survive"
        assert await fetch_one("SELECT id FROM memory_spaces WHERE name = 'dc2'") is not None

    async def test_a_space_whose_memories_are_all_forgotten_is_still_refused(
        self, client, auth_headers
    ):
        """
        The trap this endpoint is most likely to fall into.

        The space list counts only live chunks, so a space whose memories have
        all been forgotten displays as empty. The rows are still there, and
        `chunks.space_id` is NO ACTION, so the delete would be refused by
        Postgres and surface as a 500. Checking the displayed count would let
        it pass its own check and fail in the database.
        """
        space_id = await _make_space(client, auth_headers, "dc3")
        await execute_query(
            "INSERT INTO chunks (space_id, content, chunk_index, importance, metadata) "
            "VALUES (%s, 'forgotten content', 0, 0, '{\"forgotten\": true}'::jsonb)",
            (space_id,),
        )

        listing = await client.get("/api/spaces", headers=auth_headers)
        shown = [s for s in listing.json()["spaces"] if s["name"] == "dc3"][0]
        assert shown["chunk_count"] == 0, "precondition: it displays as empty"

        r = await client.delete("/api/spaces/dc3", headers=auth_headers)
        assert r.status_code == 409, f"expected a refusal, got {r.status_code}: {r.text}"


class TestASpaceWithGraphEntitiesIsRefused:
    """
    `entities.space_id` is ON DELETE CASCADE, so this case fails quietly rather
    than loudly: the delete succeeds and takes the space's entities, mentions
    and relationships with it. A check that counted only chunks would let it.
    """

    async def test_a_space_holding_only_entities_is_refused(self, client, auth_headers):
        space_id = await _make_space(client, auth_headers, "de1")
        await _add_entity(space_id)

        r = await client.delete("/api/spaces/de1", headers=auth_headers)
        assert r.status_code == 409, r.text
        assert "graph entities" in r.json()["detail"]

    async def test_the_entity_survives_the_refusal(self, client, auth_headers):
        space_id = await _make_space(client, auth_headers, "de2")
        entity_id = await _add_entity(space_id)

        await client.delete("/api/spaces/de2", headers=auth_headers)

        row = await fetch_one("SELECT id FROM entities WHERE id = %s", (entity_id,))
        assert row is not None, "the entity must not have been cascaded away"

    async def test_the_message_names_both_kinds_of_content(self, client, auth_headers):
        """
        A caller told only "not empty" has to guess what to clear. Chunks and
        entities are cleared in different places, so the count of each is
        worth saying.
        """
        space_id = await _make_space(client, auth_headers, "de3")
        await client.post(
            "/api/ingest/text",
            json={"text": "Alice works on Widget.", "space": "de3"},
            headers=auth_headers,
        )
        await _add_entity(space_id, "ExtraWidget")

        r = await client.delete("/api/spaces/de3", headers=auth_headers)
        detail = r.json()["detail"]
        assert "memories" in detail and "graph entities" in detail, detail


class TestReservedNames:
    @pytest.mark.parametrize("name", sorted(RESERVED_SPACE_NAMES))
    async def test_no_reserved_name_can_be_deleted(self, client, auth_headers, name):
        """
        Parametrized over the real set rather than a sample, so adding a
        reserved name cannot quietly leave it deletable.

        Two refusal codes, and which one depends on the name's shape rather
        than on a decision made here. `_internal` starts with an underscore,
        which the path pattern rejects before the handler runs — 422. The rest
        are well-formed names that are simply not allowed to go — 403. What
        matters for every one of them is that nothing is deleted, so that is
        what this asserts; the specific codes are pinned separately below.
        """
        r = await client.delete(f"/api/spaces/{name}", headers=auth_headers)
        assert r.status_code in (403, 422), f"{name} should be undeletable, got {r.status_code}"
        assert r.status_code != 204, f"{name} was deleted"

    @pytest.mark.parametrize("name", sorted(n for n in RESERVED_SPACE_NAMES if n != "_internal"))
    async def test_a_well_formed_reserved_name_is_403(self, client, auth_headers, name):
        r = await client.delete(f"/api/spaces/{name}", headers=auth_headers)
        assert r.status_code == 403, r.text

    async def test_the_underscore_reserved_name_is_rejected_as_malformed(
        self, client, auth_headers
    ):
        """
        `_internal` cannot name a space at all — the create path applies the
        same pattern — so 422 is the truthful answer and the endpoint is not
        loosened to produce a prettier one.
        """
        assert "_internal" in RESERVED_SPACE_NAMES

        r = await client.delete("/api/spaces/_internal", headers=auth_headers)
        assert r.status_code == 422, r.text

    async def test_the_default_space_still_exists_afterwards(self, client, auth_headers):
        await client.delete("/api/spaces/default", headers=auth_headers)

        row = await fetch_one("SELECT id FROM memory_spaces WHERE name = 'default'")
        assert row is not None, "the default space must survive"

    async def test_reserved_is_refused_before_existence_is_considered(self, client, auth_headers):
        """
        Most reserved names have no row at all. Answering 404 for those would
        say "this name is free", which is the opposite of the truth — creating
        them is refused too.
        """
        assert "admin" in RESERVED_SPACE_NAMES
        assert await fetch_one("SELECT id FROM memory_spaces WHERE name = 'admin'") is None

        r = await client.delete("/api/spaces/admin", headers=auth_headers)
        assert r.status_code == 403, r.text


class TestMissingAndMalformedNames:
    async def test_an_unknown_space_is_404(self, client, auth_headers):
        r = await client.delete("/api/spaces/never-existed", headers=auth_headers)
        assert r.status_code == 404, r.text

    @pytest.mark.parametrize(
        "bad",
        [
            "BadName",  # uppercase
            "-leading-hyphen",
            "with_underscore",
            "with space",
            "a" * 65,  # over the length limit
        ],
    )
    async def test_a_name_that_could_not_exist_is_rejected(self, client, auth_headers, bad):
        """
        The same shape rule the create endpoint applies. A name that cannot be
        created cannot name a space, so this is a malformed request rather
        than a lookup that missed.
        """
        r = await client.delete(f"/api/spaces/{bad}", headers=auth_headers)
        assert r.status_code == 422, f"{bad!r} -> {r.status_code}"


class TestAuthentication:
    async def test_deleting_requires_a_token(self, client, auth_headers):
        await _make_space(client, auth_headers, "da1")

        r = await client.delete("/api/spaces/da1")
        assert r.status_code in (401, 403), r.text

        assert await fetch_one("SELECT id FROM memory_spaces WHERE name = 'da1'") is not None


class TestConcurrentIngestDuringDelete:
    """
    The check and the delete are one transaction, and the space row is locked
    `FOR UPDATE` before counting. Both parts matter, and the lock is the part
    that is easy to write and easy to leave decorative.

    Postgres runs at read committed here, so the transaction alone does not
    stop a concurrent INSERT committing between the COUNT and the DELETE. What
    the lock does is make the other session wait: inserting a chunk has to
    validate the foreign key against the space row, which needs a shared lock
    on it, so the writer blocks until the delete commits and then finds the
    space gone. The insert is refused instead of the delete failing.
    """

    async def test_the_delete_waits_for_a_concurrent_writer_holding_the_space(
        self, client, auth_headers
    ):
        """
        Driven by locks rather than by sleeps.

        `delete_space` takes 1.5 ms, so a test that waits a fixed moment and
        then writes always arrives after it has finished — the first version of
        this test did exactly that, and the lock could be deleted from the
        source with every test still passing. Instead the test holds the space
        row itself, which forces the delete to block, and releases it only
        after a chunk has been inserted. That interleaving is guaranteed, not
        hoped for.

        With the lock in place the delete waits, sees the chunk, and refuses
        with SpaceNotEmpty. Without it the delete counts an empty space before
        the writer commits and then fails on the foreign key — a 500 rather
        than an answer.
        """
        import asyncio

        from memory_vault.models.db import get_pool
        from memory_vault.services.spaces import SpaceNotEmpty, delete_space

        space_id = await _make_space(client, auth_headers, "race1")
        pool = await get_pool()

        holder_ready = asyncio.Event()
        chunk_written = asyncio.Event()

        async def _hold_the_space_then_add_a_chunk():
            async with pool.connection() as conn:
                async with conn.transaction():
                    await conn.execute(
                        "SELECT id FROM memory_spaces WHERE id = %s FOR UPDATE", (space_id,)
                    )
                    holder_ready.set()
                    # Give delete_space time to reach its own lock and block.
                    await asyncio.sleep(0.3)
                    await conn.execute(
                        "INSERT INTO chunks (space_id, content, chunk_index) "
                        "VALUES (%s, 'arrived mid-delete', 0)",
                        (space_id,),
                    )
                    chunk_written.set()
                # Committing here releases the row and unblocks the delete.

        holder = asyncio.create_task(_hold_the_space_then_add_a_chunk())
        await holder_ready.wait()

        with pytest.raises(SpaceNotEmpty):
            await delete_space("race1")

        # Awaited, not abandoned: this is what re-raises anything the writer
        # hit. A task whose exception is never retrieved is swallowed, and the
        # test would pass whether or not the chunk was ever written.
        await asyncio.wait_for(holder, timeout=5)
        assert chunk_written.is_set(), "the racing writer never inserted its chunk"

        assert await fetch_one("SELECT id FROM memory_spaces WHERE name = 'race1'") is not None, (
            "the space must survive: a chunk was written before the delete could commit"
        )

    async def test_the_delete_does_not_leave_orphaned_chunks(self, client, auth_headers):
        """
        The failure this guards against is not a status code — it is a chunk
        row pointing at a space id that no longer exists.
        """
        space_id = await _make_space(client, auth_headers, "race2")
        await client.delete("/api/spaces/race2", headers=auth_headers)

        row = await fetch_one("SELECT COUNT(*) AS n FROM chunks WHERE space_id = %s", (space_id,))
        assert int(row["n"]) == 0, "no chunk may reference a deleted space"


class TestSchemaAssumptions:
    """
    The refusal rules exist because of these two foreign keys. If a migration
    changes either, the reasoning in `delete_space` stops holding — so the
    assumptions are asserted rather than left as a comment.
    """

    async def test_chunks_still_block_the_delete(self):
        row = await fetch_one(
            """SELECT rc.delete_rule
               FROM information_schema.table_constraints tc
               JOIN information_schema.referential_constraints rc
                 ON rc.constraint_name = tc.constraint_name
               JOIN information_schema.constraint_column_usage ccu
                 ON ccu.constraint_name = tc.constraint_name
               WHERE tc.constraint_type = 'FOREIGN KEY'
                 AND tc.table_name = 'chunks'
                 AND ccu.table_name = 'memory_spaces'"""
        )
        assert row is not None, "chunks should still reference memory_spaces"
        assert row["delete_rule"] == "NO ACTION", (
            "chunks.space_id no longer blocks a space delete — "
            "delete_space assumes Postgres refuses rather than cascades"
        )

    async def test_entities_still_cascade(self):
        row = await fetch_one(
            """SELECT rc.delete_rule
               FROM information_schema.table_constraints tc
               JOIN information_schema.referential_constraints rc
                 ON rc.constraint_name = tc.constraint_name
               JOIN information_schema.constraint_column_usage ccu
                 ON ccu.constraint_name = tc.constraint_name
               WHERE tc.constraint_type = 'FOREIGN KEY'
                 AND tc.table_name = 'entities'
                 AND ccu.table_name = 'memory_spaces'"""
        )
        assert row is not None, "entities should still reference memory_spaces"
        assert row["delete_rule"] == "CASCADE", (
            "entities.space_id no longer cascades — the entity check in "
            "delete_space exists precisely because it does"
        )
