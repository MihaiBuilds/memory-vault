"""
TDD tests for memory-vault upgrade spec (2026-06-30):
  1. Real spaces — auto-create on write; recall is a hard filter
  2. move_memory tool — change space in place, preserve chunk_id / embedding
  3. supersede_memory tool — hide old, surface new; include_superseded opt-in
  4. migrate_tags tool — move [space:X]-prefixed chunks to real spaces, strip tag

Tests are written BEFORE implementation.  Confirm RED, then implement.

Integration: requires PostgreSQL (migration 004 applied via _test_database fixture).
Run with: DB_HOST=127.0.0.1 pytest tests/test_spaces_move_supersede.py -v
"""

from __future__ import annotations

import json

import pytest
import pytest_asyncio

# ---------------------------------------------------------------------------
# Fixture: ensure server._db_ready is True so tool functions skip init_pool
# (conftest._test_database already initialised the pool)
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture(autouse=True)
async def _patch_db_ready():
    from src.mcp import server

    server._db_ready = True
    yield
    server._db_ready = False


# ---------------------------------------------------------------------------
# 1. Real spaces — auto-create on write
# ---------------------------------------------------------------------------


class TestAutoCreateSpace:
    @pytest.mark.asyncio
    async def test_remember_creates_new_space_instead_of_rejecting(self):
        from src.mcp.server import remember

        result = json.loads(await remember("payload for brand_new_space", space="brand_new_space"))
        assert result.get("stored") is True, f"expected stored=True, got {result}"
        assert result.get("space") == "brand_new_space"

    @pytest.mark.asyncio
    async def test_new_space_visible_in_memory_status(self):
        from src.mcp.server import memory_status, remember

        await remember("payload for statusspace", space="statusspace")
        status = json.loads(await memory_status())
        assert "statusspace" in status["chunks_per_space"], (
            f"statusspace not found in {list(status['chunks_per_space'].keys())}"
        )

    @pytest.mark.asyncio
    async def test_recall_from_new_space_returns_the_stored_chunk(self):
        from src.mcp.server import recall, remember

        r = json.loads(await remember("unique_token_XYZ987 recall new space", space="recallspace"))
        chunk_id = r["chunk_id"]
        result = json.loads(await recall("unique_token_XYZ987", spaces=["recallspace"]))
        ids = [item["chunk_id"] for item in result["results"]]
        assert chunk_id in ids

    @pytest.mark.asyncio
    async def test_recall_hard_filter_excludes_other_spaces(self):
        """A chunk stored in spaceA must NOT appear when recalling from spaceB."""
        from src.mcp.server import recall, remember

        r = json.loads(await remember("exclusive_ABC content in spaceA", space="spaceA"))
        chunk_id = r["chunk_id"]
        result = json.loads(await recall("exclusive_ABC content", spaces=["spaceB"]))
        ids = [item["chunk_id"] for item in result["results"]]
        assert chunk_id not in ids, "chunk from spaceA must not appear when filtering to spaceB"


# ---------------------------------------------------------------------------
# 2. move_memory
# ---------------------------------------------------------------------------


class TestMoveMemory:
    @pytest.mark.asyncio
    async def test_move_memory_changes_space(self):
        from src.mcp.server import move_memory, recall, remember

        r = json.loads(await remember("payload to be moved MOVE1"))
        chunk_id = r["chunk_id"]
        move_r = json.loads(await move_memory(chunk_id, "dest_space_1"))
        assert move_r.get("success") is True, f"move failed: {move_r}"
        result = json.loads(await recall("payload to be moved MOVE1", spaces=["dest_space_1"]))
        ids = [item["chunk_id"] for item in result["results"]]
        assert chunk_id in ids

    @pytest.mark.asyncio
    async def test_move_memory_removes_from_original_space(self):
        from src.mcp.server import move_memory, recall, remember

        r = json.loads(await remember("leave default MOVE2"))
        chunk_id = r["chunk_id"]
        await move_memory(chunk_id, "elsewhere_2")
        result = json.loads(await recall("leave default MOVE2", spaces=["default"]))
        ids = [item["chunk_id"] for item in result["results"]]
        assert chunk_id not in ids, "chunk should no longer appear under default after move"

    @pytest.mark.asyncio
    async def test_move_memory_preserves_chunk_id_and_content(self):
        from src.mcp.server import move_memory, remember

        from src.models.db import fetch_one

        r = json.loads(await remember("preserve_content MOVE3"))
        chunk_id = r["chunk_id"]
        await move_memory(chunk_id, "preserve_dest")
        row = await fetch_one("SELECT content FROM chunks WHERE id = %s", (chunk_id,))
        assert row is not None
        assert "preserve_content MOVE3" in row["content"]

    @pytest.mark.asyncio
    async def test_move_memory_preserves_embedding(self):
        """Embedding vector must be unchanged after move (no re-embed)."""
        from src.mcp.server import move_memory, remember

        from src.models.db import fetch_one

        r = json.loads(await remember("embedding preserved MOVE4"))
        chunk_id = r["chunk_id"]
        row_before = await fetch_one(
            "SELECT embedding::text AS emb FROM chunks WHERE id = %s", (chunk_id,)
        )
        await move_memory(chunk_id, "emb_dest")
        row_after = await fetch_one(
            "SELECT embedding::text AS emb FROM chunks WHERE id = %s", (chunk_id,)
        )
        assert row_before["emb"] == row_after["emb"], "embedding must not change after move"

    @pytest.mark.asyncio
    async def test_move_memory_nonexistent_chunk_returns_failure(self):
        from src.mcp.server import move_memory

        result = json.loads(await move_memory("00000000-0000-0000-0000-000000000000", "anywhere"))
        assert result.get("success") is False

    @pytest.mark.asyncio
    async def test_move_memory_auto_creates_target_space(self):
        from src.mcp.server import move_memory, remember

        from src.models.db import fetch_one

        r = json.loads(await remember("auto create target MOVE5"))
        chunk_id = r["chunk_id"]
        await move_memory(chunk_id, "autocreated_dest_space")
        row = await fetch_one(
            "SELECT id FROM memory_spaces WHERE name = %s", ("autocreated_dest_space",)
        )
        assert row is not None, "target space should have been auto-created"


# ---------------------------------------------------------------------------
# 3. supersede_memory
# ---------------------------------------------------------------------------


class TestSupersedeMemory:
    @pytest.mark.asyncio
    async def test_supersede_stores_new_chunk(self):
        from src.mcp.server import remember, supersede_memory

        r = json.loads(await remember("old fact SUP1"))
        old_id = r["chunk_id"]
        sup = json.loads(await supersede_memory(old_id, "new fact SUP1"))
        assert sup.get("stored") is True, f"expected stored=True, got {sup}"
        assert "new_chunk_id" in sup

    @pytest.mark.asyncio
    async def test_supersede_hides_old_chunk_from_default_recall(self):
        from src.mcp.server import recall, remember, supersede_memory

        r = json.loads(await remember("supersedable_QQQ old version"))
        old_id = r["chunk_id"]
        sup = json.loads(await supersede_memory(old_id, "supersedable_QQQ new version"))
        new_id = sup["new_chunk_id"]
        result = json.loads(await recall("supersedable_QQQ"))
        ids = [item["chunk_id"] for item in result["results"]]
        assert new_id in ids, "new chunk must appear in recall"
        assert old_id not in ids, "old chunk must be hidden from default recall"

    @pytest.mark.asyncio
    async def test_supersede_include_superseded_surfaces_old_chunk(self):
        from src.mcp.server import recall, remember, supersede_memory

        r = json.loads(await remember("visible_with_flag_WWW old"))
        old_id = r["chunk_id"]
        await supersede_memory(old_id, "visible_with_flag_WWW new")
        result = json.loads(await recall("visible_with_flag_WWW", include_superseded=True))
        ids = [item["chunk_id"] for item in result["results"]]
        assert old_id in ids, "old chunk must appear when include_superseded=True"

    @pytest.mark.asyncio
    async def test_supersede_nonexistent_chunk_returns_failure(self):
        from src.mcp.server import supersede_memory

        result = json.loads(
            await supersede_memory("00000000-0000-0000-0000-000000000000", "new text")
        )
        assert result.get("stored") is False

    @pytest.mark.asyncio
    async def test_supersede_inherits_space_of_old_chunk_by_default(self):
        from src.mcp.server import remember, supersede_memory

        from src.models.db import fetch_one

        await remember("", space="inherit_space")  # ensure space exists
        r = json.loads(await remember("old in inherit_space SUP2", space="inherit_space"))
        old_id = r["chunk_id"]
        sup = json.loads(await supersede_memory(old_id, "new in inherit_space SUP2"))
        new_id = sup["new_chunk_id"]
        row = await fetch_one(
            "SELECT ms.name FROM chunks c JOIN memory_spaces ms ON ms.id = c.space_id "
            "WHERE c.id = %s",
            (new_id,),
        )
        assert row["name"] == "inherit_space"

    @pytest.mark.asyncio
    async def test_supersede_with_explicit_space_uses_that_space(self):
        from src.mcp.server import remember, supersede_memory

        from src.models.db import fetch_one

        r = json.loads(await remember("old SUP3"))
        old_id = r["chunk_id"]
        sup = json.loads(await supersede_memory(old_id, "new SUP3", space="explicit_space"))
        new_id = sup["new_chunk_id"]
        row = await fetch_one(
            "SELECT ms.name FROM chunks c JOIN memory_spaces ms ON ms.id = c.space_id "
            "WHERE c.id = %s",
            (new_id,),
        )
        assert row["name"] == "explicit_space"


# ---------------------------------------------------------------------------
# 4. migrate_tags
# ---------------------------------------------------------------------------


class TestMigrateTags:
    @pytest.mark.asyncio
    async def test_migrate_moves_tagged_chunk_to_named_space(self):
        from src.mcp.server import migrate_tags, remember

        from src.models.db import fetch_one

        r = json.loads(await remember("[space:aed] AED promotion gate memory MIGT1"))
        chunk_id = r["chunk_id"]
        result = json.loads(await migrate_tags())
        assert result.get("moved", 0) >= 1
        row = await fetch_one(
            "SELECT ms.name, c.content FROM chunks c "
            "JOIN memory_spaces ms ON ms.id = c.space_id "
            "WHERE c.id = %s",
            (chunk_id,),
        )
        assert row["name"] == "aed"
        assert not row["content"].startswith("[space:")

    @pytest.mark.asyncio
    async def test_migrate_strips_space_tag_from_content(self):
        from src.mcp.server import migrate_tags, remember

        from src.models.db import fetch_one

        r = json.loads(await remember("[space:global] cross-project operator note MIGT2"))
        chunk_id = r["chunk_id"]
        await migrate_tags()
        row = await fetch_one("SELECT content FROM chunks WHERE id = %s", (chunk_id,))
        assert not row["content"].startswith("[space:")
        assert "cross-project operator note MIGT2" in row["content"]

    @pytest.mark.asyncio
    async def test_migrate_leaves_untagged_chunks_in_place(self):
        from src.mcp.server import migrate_tags, remember

        from src.models.db import fetch_one

        r = json.loads(await remember("untagged memory MIGT3"))
        chunk_id = r["chunk_id"]
        await migrate_tags()
        row = await fetch_one(
            "SELECT ms.name FROM chunks c "
            "JOIN memory_spaces ms ON ms.id = c.space_id WHERE c.id = %s",
            (chunk_id,),
        )
        assert row["name"] == "default"

    @pytest.mark.asyncio
    async def test_migrate_returns_moved_and_skipped_counts(self):
        from src.mcp.server import migrate_tags, remember

        await remember("[space:projx] tagged 1 MIGT4")
        await remember("[space:projy] tagged 2 MIGT4")
        await remember("untagged A MIGT4")
        result = json.loads(await migrate_tags())
        assert result.get("moved") == 2
        assert "skipped_untagged" in result or "skipped" in result

    @pytest.mark.asyncio
    async def test_migrate_is_idempotent(self):
        from src.mcp.server import migrate_tags, remember

        await remember("[space:idm] idempotent test MIGT5")
        await migrate_tags()
        result = json.loads(await migrate_tags())
        assert result.get("moved") == 0
