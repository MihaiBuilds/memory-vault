"""
Regression + security tests for the space-filter behavior in _build_where_clause,
plus unit coverage for has_column().

Background: recall(spaces=["unknown"]) used to silently widen to every space —
resolve_space_names() returns [] for unknown names, and every hybrid_search
caller collapsed that back to None via `or None`, so the filter disappeared and
the query hit every space in the vault. Fix: propagate [] all the way through
_build_where_clause, which now emits a hard `false` predicate for that case.

Integration: shares the memory_vault_test database from conftest.py.
"""

from __future__ import annotations

import pytest

# ---------------------------------------------------------------------------
# has_column()
# ---------------------------------------------------------------------------


class TestHasColumn:
    @pytest.mark.asyncio
    async def test_true_for_a_column_that_exists(self):
        from memory_vault.models.db import has_column

        assert await has_column("chunks", "content") is True

    @pytest.mark.asyncio
    async def test_false_for_a_column_that_does_not_exist(self):
        from memory_vault.models.db import has_column

        assert await has_column("chunks", "definitely_not_a_real_column_xyz") is False

    @pytest.mark.asyncio
    async def test_false_for_a_table_that_does_not_exist(self):
        from memory_vault.models.db import has_column

        assert await has_column("no_such_table_xyz", "id") is False


# ---------------------------------------------------------------------------
# _build_where_clause — space_ids semantics
# ---------------------------------------------------------------------------


class TestBuildWhereClause:
    def test_none_means_no_space_filter(self):
        from memory_vault.services.search import _build_where_clause

        clauses, params = _build_where_clause(None, None)
        assert not any("space_id" in c for c in clauses)
        assert not any(c == "false" for c in clauses)
        assert params == []

    def test_empty_list_emits_hard_false_predicate(self):
        """The security regression guard: caller asked for specific spaces but
        none resolved — must return zero rows, not silently widen to all spaces."""
        from memory_vault.services.search import _build_where_clause

        clauses, params = _build_where_clause([], None)
        assert "false" in clauses
        assert not any("space_id" in c for c in clauses)
        assert params == []

    def test_populated_list_filters_to_those_ids(self):
        from memory_vault.services.search import _build_where_clause

        clauses, params = _build_where_clause([1, 2, 3], None)
        assert any("c.space_id IN" in c for c in clauses)
        assert params == [1, 2, 3]
        assert not any(c == "false" for c in clauses)

    def test_forgotten_check_is_always_present(self):
        from memory_vault.services.search import _build_where_clause

        for space_ids in (None, [], [1]):
            clauses, _ = _build_where_clause(space_ids, None)
            assert "(c.metadata->>'forgotten')::boolean IS NOT TRUE" in clauses


# ---------------------------------------------------------------------------
# End-to-end via REST /api/search: unknown space name returns zero results
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
class TestSearchUnknownSpaceReturnsEmpty:
    async def test_search_with_unknown_space_returns_no_hits(self, client, auth_headers):
        """The full security regression through /api/search: a spaces=["unknown"]
        filter used to silently widen to every space because resolve_space_names
        returned [] and the caller collapsed it to None via `or None`. Now it
        must return zero hits."""
        r = await client.post(
            "/api/ingest/text",
            headers=auth_headers,
            json={
                "text": "spacefilter_unique_token_ALPHA content in default space",
                "space": "default",
            },
        )
        assert r.status_code == 200

        r = await client.post(
            "/api/search",
            headers=auth_headers,
            json={
                "query": "spacefilter_unique_token_ALPHA",
                "spaces": ["nonexistent_space_xyz"],
            },
        )
        assert r.status_code == 200
        body = r.json()
        assert body["total_results"] == 0, (
            "unknown space name must not widen to every space — "
            f"got {body['total_results']} results"
        )

    async def test_search_with_no_space_filter_still_finds_content(self, client, auth_headers):
        """Regression guard: fix must not break the no-filter path — omitting
        `spaces` (None) still searches every space as before."""
        r = await client.post(
            "/api/ingest/text",
            headers=auth_headers,
            json={
                "text": "spacefilter_unique_token_BETA content in default space",
                "space": "default",
            },
        )
        assert r.status_code == 200

        r = await client.post(
            "/api/search",
            headers=auth_headers,
            json={"query": "spacefilter_unique_token_BETA"},
        )
        assert r.status_code == 200
        assert r.json()["total_results"] >= 1
