"""
Integration tests for every REST API router.

These tests use a real test database (see conftest.py), a real FastAPI app
with its lifespan, and an httpx AsyncClient bound to the ASGI transport.
Authentication is enabled and a real token is issued per test.

Embedding runs on CPU against the loaded sentence-transformers model, so
these are genuine end-to-end tests — no mocking of the core engine.
"""

from __future__ import annotations

import io

import pytest


pytestmark = pytest.mark.asyncio


# ---------------------------------------------------------------------------
# /api/health
# ---------------------------------------------------------------------------


class TestHealth:
    async def test_health_no_auth(self, client):
        r = await client.get("/api/health")
        assert r.status_code == 200
        body = r.json()
        assert body["status"] == "ok"
        assert body["database"] == "connected"
        assert body["embedding_model"]
        assert body["version"]


# ---------------------------------------------------------------------------
# Authentication
# ---------------------------------------------------------------------------


class TestAuth:
    async def test_missing_header_returns_401(self, client):
        r = await client.get("/api/spaces")
        assert r.status_code == 401

    async def test_wrong_scheme_returns_401(self, client):
        r = await client.get("/api/spaces", headers={"Authorization": "Basic foo"})
        assert r.status_code == 401

    async def test_bogus_token_returns_401(self, client):
        r = await client.get("/api/spaces", headers={"Authorization": "Bearer mv_wrong"})
        assert r.status_code == 401

    async def test_valid_token_returns_200(self, client, auth_headers):
        r = await client.get("/api/spaces", headers=auth_headers)
        assert r.status_code == 200

    async def test_revoked_token_returns_401(self, client, auth_token):
        from src.api.deps import revoke_token

        prefix = auth_token[:11]
        ok = await revoke_token(prefix)
        assert ok is True

        r = await client.get(
            "/api/spaces",
            headers={"Authorization": f"Bearer {auth_token}"},
        )
        assert r.status_code == 401


# ---------------------------------------------------------------------------
# /api/spaces
# ---------------------------------------------------------------------------


class TestSpaces:
    async def test_default_space_seeded(self, client, auth_headers):
        r = await client.get("/api/spaces", headers=auth_headers)
        assert r.status_code == 200
        body = r.json()
        names = [s["name"] for s in body["spaces"]]
        assert "default" in names

    async def test_chunk_count_starts_at_zero(self, client, auth_headers):
        r = await client.get("/api/spaces", headers=auth_headers)
        default = next(s for s in r.json()["spaces"] if s["name"] == "default")
        assert default["chunk_count"] == 0

    async def test_create_space_then_ingest(self, client, auth_headers):
        r = await client.post(
            "/api/spaces",
            headers=auth_headers,
            json={"name": "work", "description": "work notes"},
        )
        assert r.status_code == 201
        assert r.json()["name"] == "work"
        assert r.json()["chunk_count"] == 0

        r = await client.get("/api/spaces", headers=auth_headers)
        names = [s["name"] for s in r.json()["spaces"]]
        assert "work" in names

        r = await client.post(
            "/api/ingest/text",
            headers=auth_headers,
            json={"text": "a work memory", "space": "work"},
        )
        assert r.status_code == 200

    async def test_create_duplicate_space_returns_409(self, client, auth_headers):
        r = await client.post(
            "/api/spaces", headers=auth_headers, json={"name": "default"}
        )
        assert r.status_code == 409

    async def test_create_space_invalid_name_returns_422(self, client, auth_headers):
        for bad in ["Work", "with space", "-leading", "has_underscore", ""]:
            r = await client.post(
                "/api/spaces", headers=auth_headers, json={"name": bad}
            )
            assert r.status_code == 422, f"expected 422 for {bad!r}"


# ---------------------------------------------------------------------------
# /api/ingest/text
# ---------------------------------------------------------------------------


class TestIngestText:
    async def test_ingest_returns_chunk_id(self, client, auth_headers):
        r = await client.post(
            "/api/ingest/text",
            headers=auth_headers,
            json={"text": "the sky is blue", "space": "default"},
        )
        assert r.status_code == 200
        body = r.json()
        assert body["stored"] is True
        assert body["chunk_id"]
        assert body["chunks_created"] == 1

    async def test_ingest_unknown_space_returns_404(self, client, auth_headers):
        r = await client.post(
            "/api/ingest/text",
            headers=auth_headers,
            json={"text": "hello", "space": "no_such_space"},
        )
        assert r.status_code == 404

    async def test_ingest_empty_text_rejected(self, client, auth_headers):
        r = await client.post(
            "/api/ingest/text",
            headers=auth_headers,
            json={"text": "", "space": "default"},
        )
        assert r.status_code == 422

    async def test_ingest_persists_speaker(self, client, auth_headers):
        r = await client.post(
            "/api/ingest/text",
            headers=auth_headers,
            json={"text": "note from mihai", "space": "default", "speaker": "mihai"},
        )
        assert r.status_code == 200
        chunk_id = r.json()["chunk_id"]

        got = await client.get(f"/api/chunks/{chunk_id}", headers=auth_headers)
        assert got.status_code == 200
        assert got.json()["speaker"] == "mihai"


# ---------------------------------------------------------------------------
# /api/ingest/file
# ---------------------------------------------------------------------------


class TestIngestFile:
    async def test_upload_markdown_file(self, client, auth_headers):
        content = b"# Hello\n\nThis is a markdown file for testing the ingestion pipeline.\n"
        files = {"file": ("test.md", io.BytesIO(content), "text/markdown")}
        data = {"space": "default"}
        r = await client.post("/api/ingest/file", headers=auth_headers, files=files, data=data)
        assert r.status_code == 200
        body = r.json()
        assert body["stored"] is True
        assert body["chunks_created"] >= 1

    async def test_upload_to_unknown_space(self, client, auth_headers):
        files = {"file": ("test.txt", io.BytesIO(b"hello"), "text/plain")}
        data = {"space": "no_such_space"}
        r = await client.post("/api/ingest/file", headers=auth_headers, files=files, data=data)
        assert r.status_code == 404


# ---------------------------------------------------------------------------
# /api/search
# ---------------------------------------------------------------------------


class TestSearch:
    async def test_search_finds_ingested_chunk(self, client, auth_headers):
        await client.post(
            "/api/ingest/text",
            headers=auth_headers,
            json={
                "text": "Memory Vault uses PostgreSQL with pgvector for hybrid search",
                "space": "default",
            },
        )

        r = await client.post(
            "/api/search",
            headers=auth_headers,
            json={"query": "hybrid search pgvector", "limit": 5},
        )
        assert r.status_code == 200
        body = r.json()
        assert body["total_results"] >= 1
        assert "pgvector" in body["results"][0]["content"]
        assert body["query_time_ms"] >= 0
        assert len(body["query_variations"]) >= 1

    async def test_search_empty_db_returns_zero(self, client, auth_headers):
        r = await client.post(
            "/api/search",
            headers=auth_headers,
            json={"query": "anything", "limit": 5},
        )
        assert r.status_code == 200
        assert r.json()["total_results"] == 0

    async def test_search_invalid_date_rejected(self, client, auth_headers):
        r = await client.post(
            "/api/search",
            headers=auth_headers,
            json={"query": "x", "since": "not-a-date"},
        )
        assert r.status_code == 400

    async def test_search_limit_validation(self, client, auth_headers):
        r = await client.post(
            "/api/search",
            headers=auth_headers,
            json={"query": "x", "limit": 999},
        )
        assert r.status_code == 422


# ---------------------------------------------------------------------------
# /api/chunks — list, get, delete
# ---------------------------------------------------------------------------


class TestChunks:
    async def _ingest(self, client, auth_headers, text: str) -> str:
        r = await client.post(
            "/api/ingest/text",
            headers=auth_headers,
            json={"text": text, "space": "default"},
        )
        return r.json()["chunk_id"]

    async def test_list_empty(self, client, auth_headers):
        r = await client.get("/api/chunks", headers=auth_headers)
        assert r.status_code == 200
        body = r.json()
        assert body["total"] == 0
        assert body["chunks"] == []

    async def test_list_with_pagination(self, client, auth_headers):
        for i in range(3):
            await self._ingest(client, auth_headers, f"chunk number {i}")

        r = await client.get("/api/chunks?limit=2&offset=0", headers=auth_headers)
        body = r.json()
        assert body["total"] == 3
        assert len(body["chunks"]) == 2
        assert body["limit"] == 2

        r = await client.get("/api/chunks?limit=2&offset=2", headers=auth_headers)
        assert len(r.json()["chunks"]) == 1

    async def test_list_filter_by_space(self, client, auth_headers):
        await self._ingest(client, auth_headers, "in default space")
        r = await client.get("/api/chunks?space=default", headers=auth_headers)
        assert r.status_code == 200
        assert r.json()["total"] == 1

        r = await client.get("/api/chunks?space=nonexistent", headers=auth_headers)
        assert r.json()["total"] == 0

    async def test_get_chunk_by_id(self, client, auth_headers):
        chunk_id = await self._ingest(client, auth_headers, "find me by id")
        r = await client.get(f"/api/chunks/{chunk_id}", headers=auth_headers)
        assert r.status_code == 200
        assert r.json()["content"] == "find me by id"

    async def test_get_unknown_chunk_returns_404(self, client, auth_headers):
        r = await client.get(
            "/api/chunks/00000000-0000-0000-0000-000000000000",
            headers=auth_headers,
        )
        assert r.status_code == 404

    async def test_delete_chunk_soft_deletes(self, client, auth_headers):
        chunk_id = await self._ingest(
            client, auth_headers, "this chunk will be forgotten"
        )

        r = await client.delete(f"/api/chunks/{chunk_id}", headers=auth_headers)
        assert r.status_code == 200
        assert r.json()["success"] is True

        # Still retrievable by id (soft delete)
        r = await client.get(f"/api/chunks/{chunk_id}", headers=auth_headers)
        assert r.status_code == 200

        # But no longer in default listings
        r = await client.get("/api/chunks", headers=auth_headers)
        assert r.json()["total"] == 0

        # Nor in search results
        r = await client.post(
            "/api/search",
            headers=auth_headers,
            json={"query": "forgotten chunk", "limit": 5},
        )
        assert r.json()["total_results"] == 0

    async def test_delete_twice_returns_conflict(self, client, auth_headers):
        chunk_id = await self._ingest(client, auth_headers, "delete twice")
        await client.delete(f"/api/chunks/{chunk_id}", headers=auth_headers)
        r = await client.delete(f"/api/chunks/{chunk_id}", headers=auth_headers)
        assert r.status_code == 409

    async def test_delete_unknown_chunk_returns_404(self, client, auth_headers):
        r = await client.delete(
            "/api/chunks/00000000-0000-0000-0000-000000000000",
            headers=auth_headers,
        )
        assert r.status_code == 404

    async def test_list_include_forgotten(self, client, auth_headers):
        chunk_id = await self._ingest(client, auth_headers, "also forgotten")
        await client.delete(f"/api/chunks/{chunk_id}", headers=auth_headers)

        r = await client.get("/api/chunks?include_forgotten=true", headers=auth_headers)
        assert r.json()["total"] == 1


# ---------------------------------------------------------------------------
# /docs + openapi
# ---------------------------------------------------------------------------


class TestDocs:
    async def test_openapi_schema_served(self, client):
        r = await client.get("/openapi.json")
        assert r.status_code == 200
        schema = r.json()
        assert schema["info"]["title"] == "Memory Vault API"
        paths = schema["paths"]
        assert "/api/health" in paths
        assert "/api/search" in paths
        assert "/api/chunks" in paths
        assert "/api/ingest/text" in paths

    async def test_docs_html_served(self, client):
        r = await client.get("/docs")
        assert r.status_code == 200
        assert "text/html" in r.headers.get("content-type", "")
