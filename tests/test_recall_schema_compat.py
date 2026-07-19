"""
Interim schema-compatibility tests (2026-07-03).

migrations/004_supersede_move.sql (is_superseded / superseded_by) has NOT been
applied to the live production database — that's a separate, deliberate task
scheduled later (MEMORY_VAULT_UPGRADE_SPEC). Until then:

  * recall() must not hard-reference `is_superseded` — it errored in production
    with "column c.is_superseded does not exist". Fixed via has_column() +
    an importance>0 interim proxy (the same signal memory_status()'s "active"
    count already uses).
  * supersede_memory / move_memory / migrate_tags / _ensure_space's auto-create
    are all "ahead of the schema" and must fail gracefully (a clear message,
    zero writes) rather than error or half-write, when the column is missing.

These tests run against the SAME migrated `memory_vault_test` database as the
rest of the suite (see conftest.py) — has_column() genuinely returns True
there, so the "column missing" scenarios below mock has_column() directly
rather than standing up a second, un-migrated test database. This also proves
the existing test_spaces_move_supersede.py suite's "migrated" expectations are
untouched: those tests keep passing unchanged because has_column() truthfully
reports the column exists in THIS database.

Integration: requires PostgreSQL (see conftest.py's _test_database fixture).
"""

from __future__ import annotations

import json

import pytest


async def _false(table: str, column: str) -> bool:
    return False


async def _true(table: str, column: str) -> bool:
    return True


# ---------------------------------------------------------------------------
# has_column() itself
# ---------------------------------------------------------------------------


class TestHasColumn:
    @pytest.mark.asyncio
    async def test_true_for_a_column_that_exists(self):
        from src.models.db import has_column

        assert await has_column("chunks", "is_superseded") is True

    @pytest.mark.asyncio
    async def test_false_for_a_column_that_does_not_exist(self):
        from src.models.db import has_column

        assert await has_column("chunks", "definitely_not_a_real_column_xyz") is False

    @pytest.mark.asyncio
    async def test_false_for_a_table_that_does_not_exist(self):
        from src.models.db import has_column

        assert await has_column("no_such_table_xyz", "id") is False


# ---------------------------------------------------------------------------
# _build_where_clause — the recall() fix itself
# ---------------------------------------------------------------------------


class TestBuildWhereClause:
    @pytest.mark.asyncio
    async def test_uses_is_superseded_when_column_exists(self, monkeypatch):
        from src.services import search

        monkeypatch.setattr(search, "has_column", _true)
        clauses, _params = await search._build_where_clause(None, None, include_superseded=False)
        assert "c.is_superseded = false" in clauses
        assert not any("importance" in c for c in clauses)

    @pytest.mark.asyncio
    async def test_uses_importance_proxy_when_column_missing(self, monkeypatch):
        """The exact regression guard for the production bug: recall() must NEVER emit
        `is_superseded` in its SQL when the column doesn't exist on this database."""
        from src.services import search

        monkeypatch.setattr(search, "has_column", _false)
        clauses, _params = await search._build_where_clause(None, None, include_superseded=False)
        assert "c.importance > 0" in clauses
        assert not any("is_superseded" in c for c in clauses)

    @pytest.mark.asyncio
    async def test_include_superseded_skips_both_checks_regardless_of_schema(self, monkeypatch):
        from src.services import search

        for fake in (_true, _false):
            monkeypatch.setattr(search, "has_column", fake)
            clauses, _params = await search._build_where_clause(None, None, include_superseded=True)
            joined = " ".join(clauses)
            assert "is_superseded" not in joined
            assert "importance" not in joined

    @pytest.mark.asyncio
    async def test_forgotten_check_is_always_present_regardless_of_schema(self, monkeypatch):
        from src.services import search

        for fake in (_true, _false):
            monkeypatch.setattr(search, "has_column", fake)
            clauses, _params = await search._build_where_clause(
                None, None, include_superseded=False
            )
            assert "(c.metadata->>'forgotten')::boolean IS NOT TRUE" in clauses


# ---------------------------------------------------------------------------
# recall() / memory_status() — end-to-end, must work against this database
# ---------------------------------------------------------------------------


class TestRecallAndStatusWork:
    @pytest.mark.asyncio
    async def test_recall_returns_ranked_results(self):
        from src.mcp.server import recall, remember

        await remember("unique_token_SCHEMACOMPAT1 a fact about recall working")
        result = json.loads(await recall("unique_token_SCHEMACOMPAT1"))
        assert result["status"] == "ok"
        assert len(result["results"]) >= 1

    @pytest.mark.asyncio
    async def test_recall_excludes_zero_importance_chunks_when_column_missing(self, monkeypatch):
        """Same behavior recall() already has via is_superseded, reached through the interim
        importance>0 proxy: a forgotten (importance=0) chunk stays hidden by default."""
        from src.mcp import server
        from src.services import search

        r = json.loads(await server.remember("unique_token_SCHEMACOMPAT2 forgettable"))
        chunk_id = r["chunk_id"]
        await server.forget(chunk_id)

        monkeypatch.setattr(search, "has_column", _false)
        result = json.loads(await server.recall("unique_token_SCHEMACOMPAT2"))
        ids = [item["chunk_id"] for item in result["results"]]
        assert chunk_id not in ids

    @pytest.mark.asyncio
    async def test_memory_status_stays_online(self):
        from src.mcp.server import memory_status

        status = json.loads(await memory_status())
        assert status["status"] == "online"
        assert status["database"] == "connected"
        assert isinstance(status["total_chunks"], int)
        assert isinstance(status["active_chunks"], int)


# ---------------------------------------------------------------------------
# Guards: supersede_memory / move_memory / migrate_tags / auto-create space
# ---------------------------------------------------------------------------


class TestGuardsWhenSchemaNotReady:
    @pytest.mark.asyncio
    async def test_move_memory_guarded(self, monkeypatch):
        from src.mcp import server

        r = json.loads(await server.remember("payload for move guard test GUARDMOVE1"))
        chunk_id = r["chunk_id"]

        monkeypatch.setattr(server, "has_column", _false)
        result = json.loads(await server.move_memory(chunk_id, "some_new_space"))
        assert result.get("success") is False
        assert "migration" in result.get("error", "").lower()

    @pytest.mark.asyncio
    async def test_supersede_memory_guarded(self, monkeypatch):
        from src.mcp import server

        r = json.loads(await server.remember("payload for supersede guard GUARDSUP1"))
        old_id = r["chunk_id"]

        monkeypatch.setattr(server, "has_column", _false)
        result = json.loads(await server.supersede_memory(old_id, "new text GUARDSUP1"))
        assert result.get("stored") is False
        assert "migration" in result.get("error", "").lower()

    @pytest.mark.asyncio
    async def test_supersede_memory_no_half_write_when_guarded(self, monkeypatch):
        """The precise concern the task named: supersede_memory must not create a new chunk
        and THEN fail — it must do zero writes when the schema isn't ready."""
        from src.mcp import server
        from src.models.db import fetch_one

        r = json.loads(await server.remember("payload for half-write check GUARDSUP2"))
        old_id = r["chunk_id"]
        before = await fetch_one("SELECT COUNT(*) AS n FROM chunks")

        monkeypatch.setattr(server, "has_column", _false)
        await server.supersede_memory(old_id, "new text GUARDSUP2")

        after = await fetch_one("SELECT COUNT(*) AS n FROM chunks")
        assert after["n"] == before["n"], "no new chunk should be created (no half-write)"

    @pytest.mark.asyncio
    async def test_migrate_tags_guarded(self, monkeypatch):
        from src.mcp import server

        monkeypatch.setattr(server, "has_column", _false)
        result = json.loads(await server.migrate_tags())
        assert "migration" in result.get("error", "").lower()

    @pytest.mark.asyncio
    async def test_remember_new_space_guarded(self, monkeypatch):
        from src.mcp import server

        monkeypatch.setattr(server, "has_column", _false)
        result = json.loads(
            await server.remember("payload GUARDSPACE1", space="brand_new_space_guard1")
        )
        assert result.get("stored") is False
        assert "migration" in result.get("error", "").lower()

    @pytest.mark.asyncio
    async def test_remember_existing_default_space_still_works_when_guarded(self, monkeypatch):
        """The guard blocks CREATING new spaces, not using the one that already exists."""
        from src.mcp import server

        monkeypatch.setattr(server, "has_column", _false)
        result = json.loads(await server.remember("payload GUARDSPACE2 default still works"))
        assert result.get("stored") is True
        assert result.get("space") == "default"
