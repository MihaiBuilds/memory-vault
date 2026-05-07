"""
Unicode + large-content edge-case tests.

The Polish plan calls out: "very long chunks (>20k chars), unicode/emoji/
non-Latin" must round-trip cleanly. These tests ingest realistic
boundary-case content and confirm the chunk persists, is searchable, and
comes back unchanged.
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.asyncio


SAMPLE_UNICODE = (
    # Multi-script: Romanian, Japanese, Arabic, Hebrew, Russian
    "Bună ziua de la Cluj-Napoca. "
    "こんにちは世界。日本語のテストです。"
    "مرحبا بالعالم. "
    "שלום עולם. "
    "Привет, мир!"
    # Emoji + ZWJ sequences + flags
    " 👨‍👩‍👧‍👦 🇷🇴 🚀✨ 🤖 "
    # Mathematical / combining marks
    " ∑ ∫ ∞ ≠ ≥ café naïve "
)


class TestUnicodeIngestion:
    async def test_unicode_text_round_trips(self, client, auth_headers):
        r = await client.post(
            "/api/ingest/text",
            headers=auth_headers,
            json={"text": SAMPLE_UNICODE, "space": "default", "speaker": "test"},
        )
        assert r.status_code == 200, r.text
        chunk_id = r.json()["chunk_id"]

        # Round-trip via /api/chunks/{id}
        got = await client.get(f"/api/chunks/{chunk_id}", headers=auth_headers)
        assert got.status_code == 200
        assert got.json()["content"] == SAMPLE_UNICODE

    async def test_unicode_text_is_searchable(self, client, auth_headers):
        await client.post(
            "/api/ingest/text",
            headers=auth_headers,
            json={
                "text": "Cluj-Napoca este în Transilvania 🇷🇴",
                "space": "default",
                "speaker": "test",
            },
        )
        r = await client.post(
            "/api/search",
            headers=auth_headers,
            json={"query": "Cluj-Napoca", "limit": 5},
        )
        assert r.status_code == 200
        hits = r.json()["results"]
        assert any("Cluj" in h["content"] for h in hits)


class TestLargeContent:
    async def test_long_chunk_above_20k_chars(self, client, auth_headers):
        # 25k-char paragraph — well above the plan's 20k threshold but below
        # the 1M cap. Chunker should accept and ingestion should succeed.
        big = ("memory vault stress test paragraph. " * 700)[:25_000]
        r = await client.post(
            "/api/ingest/text",
            headers=auth_headers,
            json={"text": big, "space": "default", "speaker": "test"},
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["stored"] is True

    async def test_oversized_text_rejected_with_422(self, client, auth_headers):
        too_big = "x" * 1_500_000
        r = await client.post(
            "/api/ingest/text",
            headers=auth_headers,
            json={"text": too_big, "space": "default"},
        )
        assert r.status_code == 422
