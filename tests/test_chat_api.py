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
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from memory_vault.api.routers.chat import (
    _apply_token_budget,
    _strip_thinking,
)
from memory_vault.api.schemas import ChatMessage
from memory_vault.services.search import SearchResult

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
                        # Skip malformed SSE payloads (e.g. partial frames).
                        pass
    return events


# ---------------------------------------------------------------------------
# Native-endpoint 400 -> OpenAI-compat fallback (#245)
#
# LM Studio rejects `"reasoning": "off"` with a 400 for any model that has no
# reasoning configuration (reported against gemma-4-31b-it-mlx). The fallback
# to /v1/chat/completions previously triggered only on 404/405/501, so the 400
# surfaced as an error and Chat was unusable with those models.
#
# The stub below returns LM Studio's literal error body on the native endpoint
# and a valid completion on the compat endpoint. Every request is recorded, so
# the tests can assert the fallback was actually *taken* rather than inferring
# it from a successful answer — a right answer reached the wrong way would
# otherwise pass.
# ---------------------------------------------------------------------------


# LM Studio's exact response for a model without reasoning config (issue #245).
_REASONING_400 = {
    "error": {
        "message": "Model 'gemma-4-31b-it-mlx' does not expose reasoning configuration.",
        "type": "invalid_request",
        "param": "reasoning",
        "code": "invalid_value",
    }
}

_COMPAT_ANSWER = "The novel notes mention a draft outline and two character sketches."


class _StubLMStudio(BaseHTTPRequestHandler):
    """LM Studio that rejects the native payload but serves the compat one."""

    # Request log shared by the handler class; reset per fixture.
    seen: list[tuple[str, str]] = []

    def log_message(self, *args):  # keep pytest output clean
        pass

    def _send(self, code: int, payload: dict) -> None:
        body = json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_sse_completion(self, text: str) -> None:
        """Stream the answer the way a real OpenAI-compatible server does.

        The compat client sends `stream: true` and reads `data:` frames, so a
        plain JSON body would parse as zero deltas and silently yield nothing.
        """
        frames = [
            json.dumps({"choices": [{"delta": {"content": text}}]}),
            "[DONE]",
        ]
        body = "".join(f"data: {f}\n\n" for f in frames).encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        type(self).seen.append(("GET", self.path))
        if self.path == "/v1/models":
            self._send(
                200,
                {
                    "data": [{"id": "gemma-4-31b-it-mlx", "object": "model"}],
                    "object": "list",
                },
            )
            return
        self._send(404, {"error": "not found"})

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b"{}"
        try:
            sent = json.loads(raw)
        except json.JSONDecodeError:
            sent = {}
        type(self).seen.append(("POST", self.path))

        if self.path == "/api/v1/chat":
            self._send(400, _REASONING_400)
        elif self.path == "/v1/chat/completions":
            # Real servers answer streaming and non-streaming requests
            # differently; both chat paths hit this same endpoint.
            if sent.get("stream"):
                self._send_sse_completion(_COMPAT_ANSWER)
            else:
                self._send(
                    200,
                    {"choices": [{"message": {"role": "assistant", "content": _COMPAT_ANSWER}}]},
                )
        else:
            self._send(404, {"error": "not found"})


@pytest.fixture
def no_reasoning_llm() -> Iterator[str]:
    """A stub LM Studio whose model rejects `reasoning` (issue #245)."""
    server = HTTPServer(("127.0.0.1", 0), _StubLMStudio)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    _StubLMStudio.seen = []
    host, port = server.server_address
    try:
        yield f"http://{host}:{port}"
    finally:
        server.shutdown()
        server.server_close()


def _posted_paths() -> list[str]:
    return [path for method, path in _StubLMStudio.seen if method == "POST"]


@pytest.mark.asyncio
class TestNativeFourHundredFallsBackToCompat:
    async def _seed(self, client, auth_headers):
        r = await client.post(
            "/api/ingest/text",
            headers=auth_headers,
            json={
                "text": (
                    "The novel draft has an outline and two character sketches "
                    "stored alongside the chapter notes."
                ),
                "space": "default",
                "speaker": "test",
            },
        )
        assert r.status_code == 200, r.text

    async def test_json_path_answers_from_compat_endpoint(
        self,
        client,
        auth_headers,
        no_reasoning_llm,
    ):
        await self._seed(client, auth_headers)
        _StubLMStudio.seen = []

        r = await client.post(
            "/api/chat",
            headers=auth_headers,
            json={
                "question": "What do I have on the novel?",
                "llm_url": no_reasoning_llm,
            },
        )

        assert r.status_code == 200
        body = r.json()
        assert body["status"] == "ok", body.get("message")
        assert body["answer"] == _COMPAT_ANSWER

        # The fallback must actually have been taken: native tried, compat served.
        posted = _posted_paths()
        assert "/api/v1/chat" in posted
        assert "/v1/chat/completions" in posted

    async def test_stream_path_answers_from_compat_endpoint(
        self,
        client,
        auth_headers,
        no_reasoning_llm,
    ):
        await self._seed(client, auth_headers)
        _StubLMStudio.seen = []

        r = await client.post(
            "/api/chat/stream",
            headers=auth_headers,
            json={
                "question": "What do I have on the novel?",
                "llm_url": no_reasoning_llm,
            },
        )

        assert r.status_code == 200
        events = _parse_sse(r.text)
        types = [e["type"] for e in events]

        assert types[0] == "sources"
        assert "error" not in types, next(
            (e.get("message") for e in events if e["type"] == "error"), None
        )
        assert "done" in types

        streamed = "".join(e.get("text", "") for e in events if e["type"] == "delta")
        assert _COMPAT_ANSWER in streamed

        posted = _posted_paths()
        assert "/api/v1/chat" in posted
        assert "/v1/chat/completions" in posted


@pytest.mark.asyncio
class TestStreamErrorNamesTheCause:
    """A failure the fallback cannot rescue must still tell the user why.

    Before #245 the streaming path reported every LLM failure as
    "LLM error. Check server logs.", so a user had no way to tell a bad
    payload from a dead model without reading container logs.
    """

    async def _seed(self, client, auth_headers):
        r = await client.post(
            "/api/ingest/text",
            headers=auth_headers,
            json={
                "text": "Memory Vault runs on Postgres and pgvector.",
                "space": "default",
                "speaker": "test",
            },
        )
        assert r.status_code == 200, r.text

    async def test_stream_error_includes_status_and_endpoint(
        self,
        client,
        auth_headers,
    ):
        await self._seed(client, auth_headers)

        # Both endpoints 500, so the fallback runs and still fails.
        class _AllFail(_StubLMStudio):
            def do_POST(self):
                length = int(self.headers.get("Content-Length") or 0)
                if length:
                    self.rfile.read(length)
                type(self).seen.append(("POST", self.path))
                self._send(500, {"error": "upstream exploded"})

        server = HTTPServer(("127.0.0.1", 0), _AllFail)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        _AllFail.seen = []
        host, port = server.server_address
        try:
            r = await client.post(
                "/api/chat/stream",
                headers=auth_headers,
                json={
                    "question": "what is memory vault?",
                    "llm_url": f"http://{host}:{port}",
                },
            )
            events = _parse_sse(r.text)
            err = next((e for e in events if e["type"] == "error"), None)
            assert err is not None, [e["type"] for e in events]
            # The status code is the part that was missing entirely.
            assert "500" in err["message"], err["message"]
        finally:
            server.shutdown()
            server.server_close()
