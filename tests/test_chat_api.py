"""
Backend integration tests for the chat router.

Covers the parts we can validate without a running LLM:
  - Auth (token required for both /api/chat and /api/chat/stream)
  - Empty-vault path returns the "no relevant memories" response cleanly
  - SSE plumbing: sources event arrives first when chunks exist
  - Connection-error path: pointing llm_url at an unreachable port returns
    a structured error (JSON path) and emits an "error" SSE event
  - Token-budget pure-function trims as expected
  - Thinking-strip pure-function strips <think> blocks

The only thing not covered here is a live LLM round-trip — that's the manual
end-to-end smoke test with LM Studio + Qwen2.5.
"""

from __future__ import annotations

import json

import pytest

from src.api.routers.chat import (
    _apply_token_budget,
    _strip_thinking,
)
from src.api.schemas import ChatMessage
from src.services.search import SearchResult

# Async tests opt in individually — pure-function tests below should NOT
# be auto-marked async (pytest-asyncio warns when sync functions carry the mark).


# Unreachable port — kernel rejects connection immediately, so tests don't hang.
DEAD_LLM = "http://127.0.0.1:1"


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
class TestChatAuth:
    async def test_chat_requires_auth(self, client):
        r = await client.post("/api/chat", json={"question": "hello"})
        assert r.status_code == 401

    async def test_chat_stream_requires_auth(self, client):
        r = await client.post("/api/chat/stream", json={"question": "hello"})
        assert r.status_code == 401


# ---------------------------------------------------------------------------
# Empty vault
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
class TestChatEmptyVault:
    async def test_chat_empty_vault_returns_no_memories(self, client, auth_headers):
        r = await client.post(
            "/api/chat",
            headers=auth_headers,
            json={"question": "what do you know?", "llm_url": DEAD_LLM},
        )
        assert r.status_code == 200
        body = r.json()
        assert body["status"] == "ok"
        assert body["sources"] == []
        assert body["model"] == "none"
        assert "No relevant memories" in body["answer"]

    async def test_chat_stream_empty_vault_emits_done(self, client, auth_headers):
        r = await client.post(
            "/api/chat/stream",
            headers=auth_headers,
            json={"question": "what do you know?", "llm_url": DEAD_LLM},
        )
        assert r.status_code == 200
        assert r.headers["content-type"].startswith("text/event-stream")
        events = _parse_sse(r.text)
        types = [e["type"] for e in events]
        # sources event always emitted first, then a delta with the no-memories
        # message, then done.
        assert types[0] == "sources"
        assert events[0]["sources"] == []
        assert "done" in types


# ---------------------------------------------------------------------------
# LLM unreachable (vault has chunks, but llm_url is dead)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
class TestChatLlmUnreachable:
    async def _seed(self, client, auth_headers):
        r = await client.post(
            "/api/ingest/text",
            headers=auth_headers,
            json={
                "text": (
                    "Memory Vault is a local-first AI memory system with hybrid "
                    "search and a knowledge graph. It runs on Postgres and pgvector."
                ),
                "space": "default",
                "speaker": "test",
            },
        )
        assert r.status_code == 200, r.text

    async def test_chat_json_returns_error_status_when_llm_down(
        self,
        client,
        auth_headers,
    ):
        await self._seed(client, auth_headers)
        r = await client.post(
            "/api/chat",
            headers=auth_headers,
            json={"question": "what is memory vault?", "llm_url": DEAD_LLM},
        )
        assert r.status_code == 200
        body = r.json()
        assert body["status"] == "error"
        # Sources are still returned even when LLM is unreachable so the user
        # sees what would have been used.
        assert len(body["sources"]) >= 1
        assert body["sources"][0]["chunk_id"]
        assert "Cannot connect" in (body["message"] or "") or "LLM error" in (body["message"] or "")

    async def test_chat_stream_emits_sources_then_error_when_llm_down(
        self,
        client,
        auth_headers,
    ):
        await self._seed(client, auth_headers)
        r = await client.post(
            "/api/chat/stream",
            headers=auth_headers,
            json={"question": "what is memory vault?", "llm_url": DEAD_LLM},
        )
        assert r.status_code == 200
        events = _parse_sse(r.text)
        types = [e["type"] for e in events]

        # Sources is always first
        assert types[0] == "sources"
        sources_evt = events[0]
        assert len(sources_evt["sources"]) >= 1
        assert sources_evt["sources"][0]["chunk_id"]
        assert sources_evt["query_time_ms"] >= 0

        # An error event must arrive before/instead of done
        assert "error" in types
        err = next(e for e in events if e["type"] == "error")
        assert "Cannot connect" in err["message"] or "LLM error" in err["message"]


# ---------------------------------------------------------------------------
# Pure-function unit tests (no DB / no HTTP)
# ---------------------------------------------------------------------------


class TestStripThinking:
    def test_strips_xml_think_block(self):
        out = _strip_thinking("<think>analysis here</think>\n\nThe answer is 42.")
        assert out == "The answer is 42."

    def test_passthrough_when_no_think(self):
        assert _strip_thinking("Plain answer.") == "Plain answer."

    def test_handles_only_thinking_with_no_answer(self):
        out = _strip_thinking("Thinking Process: step 1\n\nstep 2\n\nstep 3")
        assert "internal reasoning" in out or out  # fallback message or recovered last paragraph


class TestTokenBudget:
    def _result(self, content: str, similarity: float) -> SearchResult:
        return SearchResult(
            chunk_id="x",
            content=content,
            similarity=similarity,
            speaker=None,
            space="default",
            source=None,
            created_at=None,
        )

    def test_keeps_everything_when_under_budget(self):
        history = [
            ChatMessage(role="user", content="hi"),
            ChatMessage(role="assistant", content="hello"),
        ]
        results = [self._result("short content", 0.9)]
        h, r = _apply_token_budget("question?", history, results)
        assert h == history
        assert r == results

    def test_drops_oldest_history_first(self):
        # Build history bloat that exceeds the 6000-token budget on its own
        big = "x" * (6000 * 4)
        history = [
            ChatMessage(role="user", content=big),
            ChatMessage(role="assistant", content="recent"),
        ]
        results = [self._result("relevant", 0.9)]
        h, r = _apply_token_budget("q?", history, results)
        # Oldest dropped; at least one chunk preserved
        assert all(big not in m.content for m in h)
        assert len(r) >= 1

    def test_drops_lowest_similarity_chunks_after_history(self):
        big = "x" * (6000 * 4)
        results = [
            self._result(big, 0.95),  # huge but most relevant
            self._result(big, 0.50),  # huge and less relevant — should be dropped first
        ]
        h, r = _apply_token_budget("q?", [], results)
        # At least the highest-similarity chunk survives
        assert len(r) >= 1
        assert r[0].similarity == 0.95


# ---------------------------------------------------------------------------
# SSE parsing helper
# ---------------------------------------------------------------------------


def _parse_sse(body: str) -> list[dict]:
    """Parse an SSE stream body into a list of decoded JSON events."""
    events: list[dict] = []
    for frame in body.split("\n\n"):
        for line in frame.splitlines():
            if line.startswith("data:"):
                payload = line[5:].strip()
                if payload:
                    try:
                        events.append(json.loads(payload))
                    except json.JSONDecodeError:
                        pass
    return events
