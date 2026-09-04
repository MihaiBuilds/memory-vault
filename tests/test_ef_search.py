"""
`ef_search` as a per-query knob.

HNSW trades recall for speed: the index is searched to a breadth set by
`hnsw.ef_search`, default 40. On a large or noisy corpus that default can miss
matches a wider search would find, and there was no way to ask for a wider one
without changing the server default for everybody.

The setting has to be applied on the same connection that runs the query.
`fetch_all` takes a connection from the pool, runs one statement and hands it
back, so a separate `SET LOCAL` would land on a different connection and be
silently lost — the query would run at the default while appearing to honour
the request. `fetch_all_with_setting` is what makes it actually apply.
"""

from __future__ import annotations

import pytest

from memory_vault.models.db import fetch_all, fetch_all_with_setting
from memory_vault.services.search import _EF_SEARCH_MAX, _EF_SEARCH_MIN

pytestmark = pytest.mark.asyncio

SERVER_DEFAULT = "40"


class TestSettingIsAppliedAndScoped:
    async def test_setting_applies_to_the_statement(self):
        rows = await fetch_all_with_setting("hnsw.ef_search", "250", "SHOW hnsw.ef_search")
        assert rows[0]["hnsw.ef_search"] == "250"

    async def test_setting_does_not_leak_to_later_queries(self):
        """
        The reason this uses set_config(..., local => true) rather than a
        session SET. Connections are pooled, so a session-scoped setting stays
        on the connection and silently changes whatever query borrows it next.

        Checked repeatedly rather than once, and that detail matters: the pool
        holds several connections, so a single check can land on a clean one
        and pass while a poisoned connection is still in rotation. Verified by
        mutation — with `local => false` a single check passed and the leak
        only showed on the second borrow.
        """
        await fetch_all_with_setting("hnsw.ef_search", "500", "SHOW hnsw.ef_search")

        seen = set()
        for _ in range(10):
            rows = await fetch_all("SHOW hnsw.ef_search")
            seen.add(rows[0]["hnsw.ef_search"])

        assert seen == {SERVER_DEFAULT}, (
            f"the setting leaked into pooled connections: saw {sorted(seen)}"
        )

    async def test_value_is_bound_not_interpolated(self):
        """
        `SET LOCAL` only accepts a literal, which would mean putting a
        caller-supplied value into SQL text. set_config takes it as a
        parameter, so a hostile value is data rather than syntax.
        """
        with pytest.raises(Exception):  # noqa: B017 - any DB error proves it was not executed
            await fetch_all_with_setting(
                "hnsw.ef_search", "40; DROP TABLE chunks", "SHOW hnsw.ef_search"
            )

        rows = await fetch_all("SELECT to_regclass('public.chunks') AS t")
        assert rows[0]["t"] is not None, "chunks must still exist"


class TestSearchAcceptsTheKnob:
    async def _seed(self, client, auth_headers, space: str):
        await client.post("/api/spaces", json={"name": space}, headers=auth_headers)
        await client.post(
            "/api/ingest/text",
            json={
                "text": "Hybrid search combines vector similarity and full-text retrieval.",
                "space": space,
            },
            headers=auth_headers,
        )

    @pytest.mark.parametrize("ef", [None, 1, 100, 1000])
    async def test_valid_values_are_accepted(self, client, auth_headers, ef):
        await self._seed(client, auth_headers, "ef1")
        body = {"query": "hybrid search", "spaces": ["ef1"], "limit": 3}
        if ef is not None:
            body["ef_search"] = ef

        resp = await client.post("/api/search", json=body, headers=auth_headers)
        assert resp.status_code == 200, resp.text
        assert resp.json()["results"], "the seeded memory should still be found"

    @pytest.mark.parametrize("bad", [0, -1, 1001, 100_000])
    async def test_out_of_range_is_rejected(self, client, auth_headers, bad):
        """
        Bounded at the API boundary so a caller gets told, rather than having a
        silly value quietly clamped. A huge value is a slow scan on the
        operator's own instance.
        """
        resp = await client.post(
            "/api/search",
            json={"query": "anything", "ef_search": bad},
            headers=auth_headers,
        )
        assert resp.status_code == 422

    async def test_omitting_it_changes_nothing(self, client, auth_headers):
        """The default path must be untouched — every existing caller omits it."""
        await self._seed(client, auth_headers, "ef2")

        without = await client.post(
            "/api/search",
            json={"query": "hybrid search", "spaces": ["ef2"], "limit": 3},
            headers=auth_headers,
        )
        with_default = await client.post(
            "/api/search",
            json={"query": "hybrid search", "spaces": ["ef2"], "limit": 3, "ef_search": 40},
            headers=auth_headers,
        )

        assert without.status_code == with_default.status_code == 200
        assert [r["chunk_id"] for r in without.json()["results"]] == [
            r["chunk_id"] for r in with_default.json()["results"]
        ], "explicitly passing the server default should match omitting it"


class TestServiceClamps:
    """
    The API rejects out-of-range values, but `hybrid_search` is also called
    directly — from MCP `recall`, and from anything built on the service. It
    clamps rather than raising, because a tuning hint should not fail a search.
    """

    async def test_bounds_are_sane(self):
        assert _EF_SEARCH_MIN == 1
        assert _EF_SEARCH_MAX == 1000

    async def test_service_clamps_instead_of_failing(self, client, auth_headers):
        from memory_vault.services.search import hybrid_search

        await client.post("/api/spaces", json={"name": "ef3"}, headers=auth_headers)
        await client.post(
            "/api/ingest/text",
            json={"text": "A memory for the clamping test.", "space": "ef3"},
            headers=auth_headers,
        )

        for wild in (-5, 0, 99_999):
            results, _, _ = await hybrid_search(query_text="clamping test", ef_search=wild)
            assert isinstance(results, list), f"ef_search={wild} should not raise"
