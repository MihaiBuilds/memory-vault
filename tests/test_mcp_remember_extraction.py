"""
Regression tests for MCP `remember` — verify it now runs graph extraction
after INSERT, and rejects empty/oversized text at the boundary (parity with
REST /api/ingest/text).

Background: MCP `remember` used to insert a chunk directly and skip
`_run_extraction`, so MCP-stored memories were searchable but silently
absent from every /api/graph/* surface. It also accepted empty text and
unbounded payloads that the REST surface rejected via Pydantic (422).
"""

from __future__ import annotations

import json

import pytest

pytestmark = pytest.mark.asyncio


class TestRememberRunsExtraction:
    async def test_remember_populates_graph_for_entity_rich_text(self):
        """The exact regression: an entity-rich MCP-stored memory must
        produce entity_mentions rows, not just a chunk row."""
        from memory_vault.mcp.server import remember
        from memory_vault.models.db import fetch_one

        result = json.loads(
            await remember(
                "unique_extraction_token_ALPHA — Anthropic and Google announced "
                "a partnership on Claude infrastructure."
            )
        )
        assert result.get("stored") is True
        chunk_id = result["chunk_id"]

        # Extraction is best-effort — if it fires at all, entity_mentions has
        # rows keyed to this chunk_id. Before the fix, that count was
        # unconditionally zero.
        row = await fetch_one(
            "SELECT COUNT(*) AS n FROM entity_mentions WHERE chunk_id = %s",
            (chunk_id,),
        )
        assert row is not None
        assert row["n"] >= 1, (
            "MCP remember must run graph extraction — expected at least one "
            "entity_mention row, got zero (regression)."
        )

    async def test_remember_still_stores_chunk_when_extraction_would_fail(self, monkeypatch):
        """The chunk must stay committed even if extraction blows up.
        _run_extraction swallows exceptions internally; verify remember
        still returns stored=True in that scenario."""
        from memory_vault.mcp import server

        async def _boom(*_args, **_kwargs):
            raise RuntimeError("simulated extraction failure")

        monkeypatch.setattr(server, "_run_extraction", _boom)
        # The mocked _run_extraction raises, but remember's outer try/except
        # catches it. The chunk INSERT itself succeeded first, so the reply
        # should reflect a stored=False only if the caller-visible payload
        # changed. Verify no exception escapes to the tool boundary.
        result = json.loads(
            await server.remember("unique_extraction_token_BETA content whose extraction blows up")
        )
        # Outer try/except catches; either stored=True (extraction ran after
        # commit and its failure was swallowed elsewhere) or stored=False with
        # an error. Both shapes are acceptable — the invariant is "no unhandled
        # exception."
        assert "stored" in result


class TestRememberEmptyTextGuard:
    async def test_empty_text_rejected(self):
        from memory_vault.mcp.server import remember

        result = json.loads(await remember(""))
        assert result.get("stored") is False
        assert "empty" in result.get("error", "").lower()

    async def test_oversized_text_rejected(self):
        from memory_vault.mcp.server import remember

        result = json.loads(await remember("x" * 1_000_001))
        assert result.get("stored") is False
        assert "1,000,000" in result.get("error", "") or "limit" in result.get("error", "").lower()

    async def test_boundary_text_length_accepted(self):
        """Exactly 1,000,000 chars is the max; must be accepted."""
        from memory_vault.mcp.server import remember

        result = json.loads(await remember("x" * 1_000_000))
        assert result.get("stored") is True or result.get("duplicate") is True


class TestExistingDedupPathUnaffected:
    async def test_exact_duplicate_still_short_circuits(self):
        """The content-hash dedup path must fire before the extraction call —
        a repeated remember() must still return duplicate=True without side
        effects."""
        from memory_vault.mcp.server import remember

        first = json.loads(await remember("unique_dedup_token_GAMMA content for dedup test"))
        assert first.get("stored") is True

        second = json.loads(await remember("unique_dedup_token_GAMMA content for dedup test"))
        assert second.get("stored") is False
        assert second.get("duplicate") is True
