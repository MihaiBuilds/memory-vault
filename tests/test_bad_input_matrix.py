"""
Bad-input test matrix — assert that every public endpoint rejects malformed
input with a structured error (4xx) and never leaks a 500 stack trace.

The plan's Polish category demands: "Hit every backend endpoint with bad
input — proper HTTP codes + JSON error shapes, no stack traces."
"""

from __future__ import annotations

import io

import pytest

pytestmark = pytest.mark.asyncio


# Any 5xx is a regression — an unhandled exception escaped to the client.
def _no_5xx(status: int) -> bool:
    return status < 500


class TestBadInputIngest:
    async def test_text_ingest_empty_body(self, client, auth_headers):
        r = await client.post("/api/ingest/text", headers=auth_headers, json={})
        assert _no_5xx(r.status_code)
        assert r.status_code == 422  # Pydantic validation

    async def test_text_ingest_oversized_text(self, client, auth_headers):
        # 1.5MB > 1MB cap on IngestTextRequest.text
        r = await client.post(
            "/api/ingest/text",
            headers=auth_headers,
            json={"text": "x" * (1_500_000), "space": "default"},
        )
        assert _no_5xx(r.status_code)
        assert r.status_code == 422

    async def test_text_ingest_wrong_types(self, client, auth_headers):
        r = await client.post(
            "/api/ingest/text",
            headers=auth_headers,
            json={"text": 123, "space": ["not", "a", "string"]},
        )
        assert _no_5xx(r.status_code)
        assert r.status_code == 422

    async def test_file_ingest_empty_upload(self, client, auth_headers):
        r = await client.post(
            "/api/ingest/file",
            headers=auth_headers,
            files={"file": ("empty.txt", io.BytesIO(b""), "text/plain")},
            data={"space": "default"},
        )
        assert _no_5xx(r.status_code)
        assert r.status_code == 400


class TestBadInputSpaces:
    async def test_create_space_invalid_pattern(self, client, auth_headers):
        for bad in ["WithCaps", "with space", "_under", "has/slash", "x" * 65]:
            r = await client.post("/api/spaces", headers=auth_headers, json={"name": bad})
            assert _no_5xx(r.status_code), f"5xx leaked for {bad!r}"
            assert r.status_code == 422, f"expected 422 for {bad!r}, got {r.status_code}"

    async def test_create_space_reserved_name(self, client, auth_headers):
        for name in ("default", "system", "admin", "all", "none", "_internal"):
            r = await client.post("/api/spaces", headers=auth_headers, json={"name": name})
            assert _no_5xx(r.status_code), f"5xx leaked for {name!r}"
            # `_internal` starts with underscore so the regex pattern rejects
            # it at the schema layer (422) before our explicit check (400).
            assert r.status_code in (400, 422), (
                f"expected 400/422 for {name!r}, got {r.status_code}"
            )


class TestBadInputChunks:
    async def test_get_unknown_chunk_returns_404(self, client, auth_headers):
        r = await client.get(
            "/api/chunks/00000000-0000-0000-0000-000000000000",
            headers=auth_headers,
        )
        assert _no_5xx(r.status_code)
        assert r.status_code == 404

    async def test_list_chunks_invalid_sort(self, client, auth_headers):
        r = await client.get("/api/chunks?sort=bogus", headers=auth_headers)
        assert _no_5xx(r.status_code)
        assert r.status_code == 422

    async def test_list_chunks_negative_offset(self, client, auth_headers):
        r = await client.get("/api/chunks?offset=-1", headers=auth_headers)
        assert _no_5xx(r.status_code)
        assert r.status_code == 422

    async def test_list_chunks_limit_too_large(self, client, auth_headers):
        r = await client.get("/api/chunks?limit=999999", headers=auth_headers)
        assert _no_5xx(r.status_code)
        assert r.status_code == 422


class TestBadInputSearch:
    async def test_search_empty_query(self, client, auth_headers):
        r = await client.post(
            "/api/search",
            headers=auth_headers,
            json={"query": ""},
        )
        assert _no_5xx(r.status_code)
        assert r.status_code == 422

    async def test_search_invalid_since_format(self, client, auth_headers):
        r = await client.post(
            "/api/search",
            headers=auth_headers,
            json={"query": "anything", "since": "not-a-date"},
        )
        assert _no_5xx(r.status_code)
        assert r.status_code == 400


class TestBadInputChat:
    async def test_chat_empty_question(self, client, auth_headers):
        r = await client.post(
            "/api/chat",
            headers=auth_headers,
            json={"question": ""},
        )
        assert _no_5xx(r.status_code)
        assert r.status_code == 422

    async def test_chat_invalid_history_role(self, client, auth_headers):
        r = await client.post(
            "/api/chat",
            headers=auth_headers,
            json={
                "question": "hi",
                "history": [{"role": "system", "content": "bogus"}],
            },
        )
        assert _no_5xx(r.status_code)
        assert r.status_code == 422

    async def test_chat_oversized_limit(self, client, auth_headers):
        r = await client.post(
            "/api/chat",
            headers=auth_headers,
            json={"question": "hi", "limit": 999},
        )
        assert _no_5xx(r.status_code)
        assert r.status_code == 422


class TestBadInputGraph:
    async def test_get_unknown_entity_returns_404(self, client, auth_headers):
        r = await client.get(
            "/api/graph/entities/00000000-0000-0000-0000-000000000000",
            headers=auth_headers,
        )
        assert _no_5xx(r.status_code)
        assert r.status_code == 404

    async def test_visualize_negative_min_mentions(self, client, auth_headers):
        r = await client.get(
            "/api/graph/visualize?min_mentions=-1",
            headers=auth_headers,
        )
        assert _no_5xx(r.status_code)
        assert r.status_code == 422

    async def test_visualize_max_nodes_too_large(self, client, auth_headers):
        r = await client.get(
            "/api/graph/visualize?max_nodes=999999",
            headers=auth_headers,
        )
        assert _no_5xx(r.status_code)
        assert r.status_code == 422
