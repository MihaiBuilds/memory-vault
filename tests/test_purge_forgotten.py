"""
Purging forgotten memories.

`forget` is a soft delete — the row stays so the memory can be recovered — and
nothing ever removed it afterwards. A vault that is edited often accumulated one
dead row per edit, forever, because editing a memory means forget + remember.

`purge_forgotten` is the deliberate other half. It is never automatic: Memory
Vault runs no timer, so deleting someone's notes is something they asked for
rather than something that happened while they were not looking. The age filter
is what keeps a purge run safe right after an accidental forget.
"""

from __future__ import annotations

import json

import pytest

from memory_vault.mcp import server as mcp
from memory_vault.models.db import execute_query, fetch_one

pytestmark = pytest.mark.asyncio


async def _remember_and_get_id(text: str) -> str:
    result = json.loads(await mcp.remember(text=text))
    return result["chunk_id"]


async def _backdate_forget(chunk_id: str, days: int) -> None:
    """Move a chunk's forgotten_at into the past so age filters can be tested."""
    await execute_query(
        """UPDATE chunks
           SET metadata = jsonb_set(
               metadata, '{forgotten_at}',
               to_jsonb((now() - make_interval(days => %s))::text))
           WHERE id = %s""",
        (days, chunk_id),
    )


async def _exists(chunk_id: str) -> bool:
    row = await fetch_one("SELECT 1 AS x FROM chunks WHERE id = %s", (chunk_id,))
    return row is not None


class TestAgeFilter:
    async def test_recent_forget_survives_the_default_window(self):
        """
        The undo window. Purging immediately after an accidental forget must
        not destroy the thing that was just forgotten.
        """
        chunk_id = await _remember_and_get_id("A memory forgotten just now.")
        await mcp.forget(chunk_id=chunk_id)

        result = json.loads(await mcp.purge_forgotten())

        assert result["success"] is True
        assert await _exists(chunk_id), "a memory forgotten today must survive a 30-day purge"

    async def test_old_forget_is_purged(self):
        chunk_id = await _remember_and_get_id("A memory forgotten long ago.")
        await mcp.forget(chunk_id=chunk_id)
        await _backdate_forget(chunk_id, 60)

        result = json.loads(await mcp.purge_forgotten())

        assert result["purged"] >= 1
        assert not await _exists(chunk_id), "a memory forgotten 60 days ago should be gone"

    async def test_zero_days_purges_every_forgotten_memory(self):
        chunk_id = await _remember_and_get_id("Forgotten and purged in the same breath.")
        await mcp.forget(chunk_id=chunk_id)

        result = json.loads(await mcp.purge_forgotten(older_than_days=0))

        assert result["remaining"] == 0
        assert not await _exists(chunk_id)

    async def test_negative_days_is_rejected(self):
        result = json.loads(await mcp.purge_forgotten(older_than_days=-1))
        assert result["success"] is False
        assert "negative" in result["error"].lower()


class TestActiveMemoriesAreNeverTouched:
    """The property that makes this safe to run at all."""

    async def test_active_memory_survives_a_full_purge(self):
        keep = await _remember_and_get_id("An active memory that must not be purged.")
        drop = await _remember_and_get_id("A forgotten memory that should go.")
        await mcp.forget(chunk_id=drop)

        await mcp.purge_forgotten(older_than_days=0)

        assert await _exists(keep), "purge must never delete an active memory"
        assert not await _exists(drop)

    async def test_purge_with_nothing_forgotten_is_a_no_op(self):
        keep = await _remember_and_get_id("Nothing here has been forgotten.")

        result = json.loads(await mcp.purge_forgotten(older_than_days=0))

        assert result["success"] is True
        assert await _exists(keep)


class TestGraphRowsGoWithTheChunk:
    async def test_entity_mentions_are_removed_by_cascade(self):
        """
        entity_mentions.chunk_id is ON DELETE CASCADE, so purging a chunk takes
        its graph rows with it. Asserting it here means a schema change that
        drops the cascade cannot pass unnoticed and leave orphans behind.
        """
        chunk_id = await _remember_and_get_id("Alice and Bob discussed Postgres in Boston.")

        before = await fetch_one(
            "SELECT COUNT(*) AS n FROM entity_mentions WHERE chunk_id = %s", (chunk_id,)
        )
        if before["n"] == 0:
            pytest.skip("extraction produced no mentions for this text")

        await mcp.forget(chunk_id=chunk_id)
        await mcp.purge_forgotten(older_than_days=0)

        after = await fetch_one(
            "SELECT COUNT(*) AS n FROM entity_mentions WHERE chunk_id = %s", (chunk_id,)
        )
        assert after["n"] == 0, "graph mentions should not outlive their chunk"


class TestReporting:
    async def test_remaining_counts_what_is_left(self):
        old = await _remember_and_get_id("Old forgotten memory for the count.")
        recent = await _remember_and_get_id("Recent forgotten memory for the count.")
        await mcp.forget(chunk_id=old)
        await mcp.forget(chunk_id=recent)
        await _backdate_forget(old, 60)

        result = json.loads(await mcp.purge_forgotten(older_than_days=30))

        assert result["purged"] == 1, "only the old one should go"
        assert result["remaining"] == 1, "the recent one should still be counted"

    async def test_status_names_the_forgotten_count(self):
        """
        The number was derivable as total - active, but only by subtracting.
        Naming it is what tells an operator whether purging is worth doing.
        """
        chunk_id = await _remember_and_get_id("A memory that will show in the count.")
        before = json.loads(await mcp.memory_status())
        await mcp.forget(chunk_id=chunk_id)
        after = json.loads(await mcp.memory_status())

        assert "forgotten_chunks" in after
        assert after["forgotten_chunks"] == before["forgotten_chunks"] + 1
        assert after["active_chunks"] == before["active_chunks"] - 1
        assert after["total_chunks"] == before["total_chunks"], (
            "a soft delete should not change the total until it is purged"
        )
