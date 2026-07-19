"""
Chat with memory — RAG over hybrid search + local LLM (LM Studio for v1.0).

Two endpoints:
  POST /api/chat          → JSON, single non-streaming response
  POST /api/chat/stream   → text/event-stream (SSE), token-by-token

Both share the same RAG pipeline:
  1. Hybrid-search the user's question across selected spaces
  2. Build a context block from top-K retrieved chunks
  3. Apply token budget: drop oldest history first, then lowest-similarity chunks
  4. Call local LLM — LM Studio native API primary (with reasoning="off"),
     OpenAI-compat fallback with <think>/Thinking-Process stripping
  5. Return answer + sources (sources emitted FIRST in stream so the UI can
     render the "based on N memories" header before tokens arrive)

Hard rule: Qwen3.5 thinking models are unusable for RAG Q&A via OpenAI-compat
— always prefer LM Studio native API with reasoning="off", or use a
non-thinking model (Qwen2.5, Llama 3).
"""

from __future__ import annotations

import json
import logging
import re
import time
from collections.abc import AsyncGenerator

import httpx
from fastapi import APIRouter, Depends
from fastapi.responses import StreamingResponse

from memory_vault.api.deps import require_token
from memory_vault.api.schemas import (
    ChatMessage,
    ChatRequest,
    ChatResponse,
    ChatSource,
)
from memory_vault.services.search import SearchResult, hybrid_search, resolve_space_names

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api", tags=["chat"], dependencies=[Depends(require_token)])


# ---------------------------------------------------------------------------
# Token budget (rough char/4 estimate — no tokenizer dependency)
# ---------------------------------------------------------------------------

# Hard ceiling on the prompt we send to the LLM. Most local SLMs (7B-14B) ship
# with 4k-8k context. We target ~6000 tokens to leave headroom for the answer.
_PROMPT_TOKEN_BUDGET = 6000
_CHARS_PER_TOKEN = 4  # rough estimate, conservative for English


def _estimate_tokens(text: str) -> int:
    return max(1, len(text) // _CHARS_PER_TOKEN)


# ---------------------------------------------------------------------------
# Thinking-strip
# ---------------------------------------------------------------------------


def _strip_thinking(text: str) -> str:
    """Strip chain-of-thought from non-streamed LLM output.

    Handles XML-tagged thinking (<think>...</think>) and plain-text thinking
    blocks ("Thinking Process:", bullet-heavy analysis) that Qwen3.5 and
    similar models produce when reasoning cannot be disabled at the API level.
    """
    stripped = re.sub(r"<think>.*?</think>\s*", "", text, flags=re.DOTALL).strip()
    if stripped and not stripped.startswith("Thinking Process"):
        return stripped

    raw = text
    for marker in (
        "\n\n---\n\n",
        "\n\n**Answer",
        "\n\nBased on",
        "\n\nAccording to",
        "\n\n## Answer",
        "\n\n# Answer",
    ):
        if marker in raw:
            answer_part = raw.split(marker, 1)[1].strip()
            if answer_part:
                if marker.strip().startswith(("**", "#")):
                    return marker.strip().lstrip("-").strip() + "\n" + answer_part
                return answer_part

    if raw.startswith(("Thinking Process", "Analyze")):
        paragraphs = [p.strip() for p in raw.split("\n\n") if p.strip()]
        for p in reversed(paragraphs):
            if any(
                p.startswith(prefix)
                for prefix in (
                    "Thinking",
                    "Analyze",
                    "Scan ",
                    "Evaluate",
                    "Synthesize",
                    "* ",
                    "- ",
                    "Wait,",
                    "Actually,",
                    "Let's",
                    "However,",
                    "Looking",
                    "I need",
                    "I will",
                    "I should",
                )
            ):
                continue
            return p
        return (
            "The model produced only internal reasoning without a final answer. "
            "Try a non-thinking model (Qwen2.5, Llama 3) or LM Studio native API."
        )

    return text


# ---------------------------------------------------------------------------
# RAG pipeline (shared by JSON + SSE endpoints)
# ---------------------------------------------------------------------------


SYSTEM_PROMPT = (
    "You are a helpful assistant with access to the user's personal knowledge base. "
    "Below is context retrieved from their memory vault. Use it to answer their question. "
    "Be specific, reference the sources when relevant, and format your answer with markdown. "
    "If the context doesn't contain enough information, say so honestly."
)


def _format_context_block(results: list[SearchResult]) -> str:
    parts = []
    for i, r in enumerate(results, 1):
        parts.append(
            f"[Source {i}] (space: {r.space}, similarity: {r.similarity:.0%})\n{r.content}"
        )
    return "\n\n---\n\n".join(parts)


def _apply_token_budget(
    question: str,
    history: list[ChatMessage],
    results: list[SearchResult],
) -> tuple[list[ChatMessage], list[SearchResult]]:
    """Trim history (oldest first) then chunks (lowest similarity first) until
    the prompt fits within _PROMPT_TOKEN_BUDGET. Always keeps the system prompt,
    the current question, and at least one chunk."""
    history = list(history)
    results = sorted(results, key=lambda r: -r.similarity)  # highest first

    def total_tokens() -> int:
        ctx = _format_context_block(results)
        history_text = "\n".join(m.content for m in history)
        return (
            _estimate_tokens(SYSTEM_PROMPT)
            + _estimate_tokens(ctx)
            + _estimate_tokens(history_text)
            + _estimate_tokens(question)
            + 200  # formatting/overhead buffer
        )

    while total_tokens() > _PROMPT_TOKEN_BUDGET and len(history) > 0:
        history.pop(0)

    while total_tokens() > _PROMPT_TOKEN_BUDGET and len(results) > 1:
        results.pop()  # drop lowest-similarity tail

    return history, results


def _build_messages(
    question: str,
    history: list[ChatMessage],
    context_block: str,
) -> list[dict]:
    msgs: list[dict] = [{"role": "system", "content": SYSTEM_PROMPT}]
    for m in history:
        msgs.append({"role": m.role, "content": m.content})
    msgs.append(
        {
            "role": "user",
            "content": (
                f"## Context from memory\n\n{context_block}\n\n---\n\n## Question\n\n{question}"
            ),
        }
    )
    return msgs


def _resolve_llm_base(llm_url: str) -> str:
    raw = llm_url.rstrip("/")
    m = re.match(r"(https?://[^/]+)", raw)
    return m.group(1) if m else "http://localhost:1234"


async def _detect_model(
    llm_base: str,
    headers: dict[str, str],
    explicit: str | None,
) -> str:
    if explicit:
        return explicit
    try:
        async with httpx.AsyncClient(timeout=5) as client:
            resp = await client.get(f"{llm_base}/v1/models", headers=headers)
            if resp.status_code == 200:
                models = resp.json().get("data", [])
                if models:
                    return models[0].get("id", "default")
    except Exception:
        # Network/parse failure when probing /v1/models — fall back to "default".
        pass
    return "default"


async def _retrieve_context(
    req: ChatRequest,
) -> tuple[list[SearchResult], list[ChatMessage], int]:
    """Run hybrid search + apply token budget. Returns (chunks, trimmed_history, query_ms)."""
    space_ids = await resolve_space_names(req.spaces) if req.spaces else None
    results, _variations, query_ms = await hybrid_search(
        query_text=req.question,
        space_ids=space_ids or None,
        limit=req.limit,
    )
    history, results = _apply_token_budget(req.question, req.history, results)
    return results, history, query_ms


def _to_sources(results: list[SearchResult]) -> list[ChatSource]:
    return [
        ChatSource(
            chunk_id=r.chunk_id,
            content=r.content,
            similarity=r.similarity,
            space=r.space,
            speaker=r.speaker,
            source=r.source,
            created_at=r.created_at,
        )
        for r in results
    ]


# ---------------------------------------------------------------------------
# POST /api/chat — JSON, non-streaming
# ---------------------------------------------------------------------------


@router.post("/chat", response_model=ChatResponse)
async def chat(req: ChatRequest) -> ChatResponse:
    try:
        results, history, query_ms = await _retrieve_context(req)

        if not results:
            return ChatResponse(
                answer="No relevant memories found for your question.",
                sources=[],
                model="none",
                query_time_ms=query_ms,
                llm_time_ms=0,
            )

        context_block = _format_context_block(results)
        messages = _build_messages(req.question, history, context_block)

        headers: dict[str, str] = {"Content-Type": "application/json"}
        if req.llm_api_key:
            headers["Authorization"] = f"Bearer {req.llm_api_key}"

        llm_base = _resolve_llm_base(req.llm_url)
        model_name = await _detect_model(llm_base, headers, req.model)

        native_url = f"{llm_base}/api/v1/chat"
        openai_url = f"{llm_base}/v1/chat/completions"

        llm_start = time.time()
        try:
            async with httpx.AsyncClient(timeout=120) as client:
                use_native = True
                user_content = (
                    f"## Context from memory\n\n{context_block}\n\n"
                    f"---\n\n## Question\n\n{req.question}"
                )
                resp = await client.post(
                    native_url,
                    headers=headers,
                    json={
                        "model": model_name,
                        "system_prompt": SYSTEM_PROMPT,
                        "input": user_content,
                        "temperature": 0.3,
                        "max_output_tokens": 2000,
                        "reasoning": "off",
                        "store": False,
                    },
                )
                if resp.status_code in (404, 405, 501):
                    use_native = False
                    resp = await client.post(
                        openai_url,
                        headers=headers,
                        json={
                            "model": model_name,
                            "messages": messages,
                            "temperature": 0.3,
                            "max_tokens": 2000,
                        },
                    )

                resp.raise_for_status()
                data = resp.json()

                if use_native:
                    output_items = data.get("output", [])
                    parts = [
                        item["content"]
                        for item in output_items
                        if item.get("type") == "message" and item.get("content")
                    ]
                    if not parts:
                        return ChatResponse(
                            answer="",
                            sources=_to_sources(results),
                            model=model_name,
                            query_time_ms=query_ms,
                            llm_time_ms=int((time.time() - llm_start) * 1000),
                            status="error",
                            message="LLM returned no message content. Make sure a model is loaded in LM Studio.",
                        )
                    answer = "\n\n".join(parts)
                else:
                    if "choices" not in data or not data["choices"]:
                        return ChatResponse(
                            answer="",
                            sources=_to_sources(results),
                            model=model_name,
                            query_time_ms=query_ms,
                            llm_time_ms=int((time.time() - llm_start) * 1000),
                            status="error",
                            message="LLM returned an empty response. Make sure a model is loaded in LM Studio.",
                        )
                    answer = _strip_thinking(data["choices"][0]["message"]["content"])

        except httpx.ConnectError:
            return ChatResponse(
                answer="",
                sources=_to_sources(results),
                model=model_name,
                query_time_ms=query_ms,
                llm_time_ms=0,
                status="error",
                message=(
                    f"Cannot connect to local LLM at {llm_base}. "
                    "Make sure LM Studio is running and a model is loaded."
                ),
            )
        except Exception as e:
            return ChatResponse(
                answer="",
                sources=_to_sources(results),
                model=model_name,
                query_time_ms=query_ms,
                llm_time_ms=0,
                status="error",
                message=f"LLM error: {e}",
            )

        return ChatResponse(
            answer=answer,
            sources=_to_sources(results),
            model=model_name,
            query_time_ms=query_ms,
            llm_time_ms=int((time.time() - llm_start) * 1000),
        )

    except Exception:
        logger.exception("Chat failed")
        return ChatResponse(
            answer="",
            sources=[],
            model="unknown",
            query_time_ms=0,
            llm_time_ms=0,
            status="error",
            message="Chat failed. Check server logs.",
        )


# ---------------------------------------------------------------------------
# POST /api/chat/stream — SSE
# ---------------------------------------------------------------------------


def _sse(event: dict) -> bytes:
    return f"data: {json.dumps(event)}\n\n".encode()


async def _stream_openai_compat(
    client: httpx.AsyncClient,
    url: str,
    headers: dict,
    payload: dict,
) -> AsyncGenerator[str, None]:
    """Yield text deltas from an OpenAI-compatible streaming response.

    Buffers <think>...</think> blocks so chain-of-thought never reaches the UI.
    """
    payload = {**payload, "stream": True}
    in_think = False
    buffer = ""

    async with client.stream("POST", url, headers=headers, json=payload) as resp:
        resp.raise_for_status()
        async for line in resp.aiter_lines():
            if not line or not line.startswith("data:"):
                continue
            data = line[5:].strip()
            if data == "[DONE]":
                break
            try:
                obj = json.loads(data)
            except json.JSONDecodeError:
                continue
            choices = obj.get("choices") or []
            if not choices:
                continue
            delta = choices[0].get("delta") or {}
            piece = delta.get("content") or ""
            if not piece:
                continue

            buffer += piece
            # Drain any complete <think>...</think> blocks
            while True:
                if not in_think:
                    open_idx = buffer.find("<think>")
                    if open_idx == -1:
                        # Hold back the tail in case it's a partial "<think>"
                        if "<" in buffer[-7:]:
                            tail = buffer[-7:]
                            emit, buffer = buffer[:-7], tail
                        else:
                            emit, buffer = buffer, ""
                        if emit:
                            yield emit
                        break
                    # Emit text before the <think>
                    emit = buffer[:open_idx]
                    if emit:
                        yield emit
                    buffer = buffer[open_idx + len("<think>") :]
                    in_think = True
                else:
                    close_idx = buffer.find("</think>")
                    if close_idx == -1:
                        buffer = ""  # discard thinking content
                        break
                    buffer = buffer[close_idx + len("</think>") :].lstrip()
                    in_think = False


async def _stream_native_lmstudio(
    client: httpx.AsyncClient,
    url: str,
    headers: dict,
    payload: dict,
) -> AsyncGenerator[str, None]:
    """Stream from LM Studio native /api/v1/chat. With reasoning='off' there
    is no <think> output to filter, so we just forward content deltas."""
    payload = {**payload, "stream": True}
    async with client.stream("POST", url, headers=headers, json=payload) as resp:
        resp.raise_for_status()
        async for line in resp.aiter_lines():
            if not line or not line.startswith("data:"):
                continue
            data = line[5:].strip()
            if data == "[DONE]":
                break
            try:
                obj = json.loads(data)
            except json.JSONDecodeError:
                continue
            # LM Studio native streams events with a "type" field; message
            # deltas carry text in "delta" or "content".
            if obj.get("type") in ("message.delta", "response.output_text.delta"):
                piece = obj.get("delta") or obj.get("content") or ""
                if piece:
                    yield piece
            elif obj.get("type") == "message":
                piece = obj.get("content") or ""
                if piece:
                    yield piece


@router.post("/chat/stream")
async def chat_stream(req: ChatRequest):
    """Server-sent events: sources first, then content deltas, then done/error."""

    async def event_stream() -> AsyncGenerator[bytes, None]:
        try:
            results, history, query_ms = await _retrieve_context(req)

            sources = _to_sources(results)
            yield _sse(
                {
                    "type": "sources",
                    "sources": [s.model_dump(mode="json") for s in sources],
                    "query_time_ms": query_ms,
                }
            )

            if not results:
                yield _sse(
                    {"type": "delta", "text": "No relevant memories found for your question."}
                )
                yield _sse({"type": "done", "model": "none", "llm_time_ms": 0})
                return

            context_block = _format_context_block(results)
            messages = _build_messages(req.question, history, context_block)

            headers: dict[str, str] = {"Content-Type": "application/json"}
            if req.llm_api_key:
                headers["Authorization"] = f"Bearer {req.llm_api_key}"

            llm_base = _resolve_llm_base(req.llm_url)
            model_name = await _detect_model(llm_base, headers, req.model)
            native_url = f"{llm_base}/api/v1/chat"
            openai_url = f"{llm_base}/v1/chat/completions"

            llm_start = time.time()
            try:
                async with httpx.AsyncClient(timeout=120) as client:
                    # Try native first
                    user_content = (
                        f"## Context from memory\n\n{context_block}\n\n"
                        f"---\n\n## Question\n\n{req.question}"
                    )
                    native_payload = {
                        "model": model_name,
                        "system_prompt": SYSTEM_PROMPT,
                        "input": user_content,
                        "temperature": 0.3,
                        "max_output_tokens": 2000,
                        "reasoning": "off",
                        "store": False,
                    }
                    use_native = True
                    try:
                        async for piece in _stream_native_lmstudio(
                            client,
                            native_url,
                            headers,
                            native_payload,
                        ):
                            yield _sse({"type": "delta", "text": piece})
                    except httpx.HTTPStatusError as e:
                        if e.response.status_code in (404, 405, 501):
                            use_native = False
                        else:
                            raise

                    if not use_native:
                        openai_payload = {
                            "model": model_name,
                            "messages": messages,
                            "temperature": 0.3,
                            "max_tokens": 2000,
                        }
                        async for piece in _stream_openai_compat(
                            client,
                            openai_url,
                            headers,
                            openai_payload,
                        ):
                            yield _sse({"type": "delta", "text": piece})

                yield _sse(
                    {
                        "type": "done",
                        "model": model_name,
                        "llm_time_ms": int((time.time() - llm_start) * 1000),
                    }
                )

            except httpx.ConnectError:
                yield _sse(
                    {
                        "type": "error",
                        "message": (
                            f"Cannot connect to local LLM at {llm_base}. "
                            "Make sure LM Studio is running and a model is loaded."
                        ),
                    }
                )
            except Exception:
                logger.exception("LLM call failed during stream")
                yield _sse({"type": "error", "message": "LLM error. Check server logs."})

        except Exception:
            logger.exception("Chat stream failed")
            yield _sse({"type": "error", "message": "Chat failed. Check server logs."})

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )
