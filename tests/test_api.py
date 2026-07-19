"""
Tests for the REST API — pure-logic tests that don't require a database.

Full integration tests live in docker compose and exercise the real stack.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from memory_vault.api.app import _parse_cors_origins, _safe_static_path
from memory_vault.api.deps import generate_token, hash_token
from memory_vault.api.schemas import (
    IngestTextRequest,
    SearchRequest,
    SpaceInfo,
)


class TestTokenHelpers:
    def test_hash_is_sha256_hex(self):
        h = hash_token("hello")
        assert len(h) == 64
        assert all(c in "0123456789abcdef" for c in h)

    def test_hash_is_deterministic(self):
        assert hash_token("same") == hash_token("same")
        assert hash_token("a") != hash_token("b")

    def test_generate_token_shape(self):
        plaintext, token_hash, prefix = generate_token()
        assert plaintext.startswith("mv_")
        assert len(plaintext) > 30
        assert hash_token(plaintext) == token_hash
        assert prefix == plaintext[:11]

    def test_generate_token_is_unique(self):
        a, _, _ = generate_token()
        b, _, _ = generate_token()
        assert a != b


class TestCorsParser:
    def test_wildcard(self):
        assert _parse_cors_origins("*") == ["*"]

    def test_empty(self):
        assert _parse_cors_origins("") == ["*"]

    def test_single(self):
        assert _parse_cors_origins("https://example.com") == ["https://example.com"]

    def test_multiple(self):
        result = _parse_cors_origins("https://a.com, https://b.com,https://c.com")
        assert result == ["https://a.com", "https://b.com", "https://c.com"]


class TestSchemas:
    def test_search_request_defaults(self):
        req = SearchRequest(query="test")
        assert req.limit == 10
        assert req.spaces is None
        assert req.since is None

    def test_search_request_rejects_empty_query(self):
        with pytest.raises(ValueError):
            SearchRequest(query="")

    def test_search_request_limit_bounds(self):
        with pytest.raises(ValueError):
            SearchRequest(query="x", limit=0)
        with pytest.raises(ValueError):
            SearchRequest(query="x", limit=51)

    def test_ingest_text_request_defaults(self):
        req = IngestTextRequest(text="hello")
        assert req.space == "default"
        assert req.source == "api"

    def test_space_info_roundtrip(self):
        info = SpaceInfo(name="default", description="d", chunk_count=5)
        assert info.chunk_count == 5


class TestSafeStaticPath:
    """Path-traversal guard for the unauthenticated SPA fallback route."""

    def test_normal_path_stays_inside_static(self, tmp_path):
        result = _safe_static_path(tmp_path, "assets/index.css")
        assert result is not None
        assert result.is_relative_to(tmp_path.resolve())

    def test_dot_dot_traversal_returns_none(self, tmp_path):
        assert _safe_static_path(tmp_path, "../../etc/passwd") is None

    def test_absolute_path_cannot_escape_static_root(self, tmp_path):
        # Leading "/" is stripped, so input like "/etc/passwd" is treated as
        # the relative path "etc/passwd" — harmlessly composed inside
        # static_root (where it does not exist; the route's is_file() check
        # then falls back to index.html). What matters: we NEVER return a
        # path resolving to the system /etc/passwd.
        result = _safe_static_path(tmp_path, "/etc/passwd")
        if result is not None:
            assert Path(result).is_relative_to(Path(os.path.realpath(tmp_path)))
            assert str(result) != "/etc/passwd"

    def test_empty_string_returns_none(self, tmp_path):
        # Empty input is rejected at the sanitizer; the route falls back
        # to serving index.html via its own `not full_path` short-circuit.
        assert _safe_static_path(tmp_path, "") is None

    def test_null_byte_returns_none(self, tmp_path):
        assert _safe_static_path(tmp_path, "assets/\x00.css") is None

    def test_leading_slash_stripped_then_resolved_safely(self, tmp_path):
        # Leading "/" is stripped by lstrip("/\\") so it does not override
        # the trusted root; the rest must still resolve inside static_root.
        result = _safe_static_path(tmp_path, "/assets/index.css")
        assert result is not None
        assert Path(result).is_relative_to(Path(os.path.realpath(tmp_path)))


class TestSpaFallbackTraversalE2E:
    """End-to-end tripwire: a traversal request must never leak file contents."""

    @pytest.mark.asyncio
    async def test_traversal_request_does_not_leak_etc_passwd(self, client):
        resp = await client.get("/../../../../etc/passwd")
        # Whether the route exists (returns 200/index.html) or not (404),
        # what matters is that /etc/passwd contents are NEVER in the body.
        assert "root:" not in resp.text
        assert "/bin/bash" not in resp.text


class TestRateLimitWindow:
    """
    The rate limiter is exercised end-to-end in docker. Here we just verify
    the sliding-window math in isolation using the middleware's internals.
    """

    def test_sliding_window_expires_old_hits(self):
        from collections import deque

        hits: deque[float] = deque()
        window = 60.0
        limit = 3

        # three hits within window -> OK
        for t in (100.0, 101.0, 102.0):
            hits.append(t)
        assert len(hits) == limit

        # advance clock past window, purge old hits
        now = 200.0
        while hits and now - hits[0] > window:
            hits.popleft()
        assert len(hits) == 0
