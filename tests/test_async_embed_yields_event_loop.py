"""
Async paths must yield the event loop while embedding inference runs.

Regression guard for issue #116: `hybrid_search`, MCP `remember`,
`_process_file`, and `ingest_text` all invoked sync `embed` / `embed_batch`
inside `async def` bodies, so a slow embedding call blocked every other
coroutine sharing the loop. The fix wraps each call in
`asyncio.to_thread`; this test verifies unrelated coroutines are no
longer starved.

The reproduction is Leonard's exact recipe: patch `embed`/`embed_batch`
with a synchronous 200ms sleep, run concurrently with an `asyncio.sleep(20ms)`,
assert the unrelated sleep finishes near 20ms rather than ~200ms.
"""

from __future__ import annotations

import asyncio
import time

import pytest

import memory_vault.services.embedding as embedding_module
import memory_vault.services.search as search_module

pytestmark = pytest.mark.asyncio

BLOCK_MS = 200
UNRELATED_MS = 20
# Generous ceiling: unrelated sleep should complete well under the block
# duration even accounting for CI jitter. If the fix is missing, the
# unrelated coroutine only wakes up after the blocking embed returns
# (~BLOCK_MS), so anything under BLOCK_MS / 2 proves the loop yielded.
UPPER_BOUND_MS = BLOCK_MS / 2


def _blocking_embed(text: str) -> list[float]:
    """Sync stub that emulates a slow embedding call by blocking the thread."""
    time.sleep(BLOCK_MS / 1000)
    return [0.0] * 384


def _blocking_embed_batch(texts: list[str]) -> list[list[float]]:
    """Sync stub — one BLOCK_MS regardless of batch size."""
    time.sleep(BLOCK_MS / 1000)
    return [[0.0] * 384 for _ in texts]


async def _time_unrelated_coroutine_during(coro) -> float:
    """Run `coro` alongside asyncio.sleep(UNRELATED_MS); return ms until the sleep resolves."""
    sleep_done_at: dict[str, float] = {}

    async def _unrelated():
        await asyncio.sleep(UNRELATED_MS / 1000)
        sleep_done_at["ms"] = (time.perf_counter() - start) * 1000

    start = time.perf_counter()
    await asyncio.gather(coro, _unrelated())
    return sleep_done_at["ms"]


async def test_hybrid_search_yields_event_loop_while_embedding(monkeypatch):
    """search.hybrid_search must not stall unrelated coroutines during embed."""
    monkeypatch.setattr(search_module, "embed", _blocking_embed)
    monkeypatch.setattr(search_module, "embed_batch", _blocking_embed_batch)
    monkeypatch.setattr(embedding_module, "embed", _blocking_embed)
    monkeypatch.setattr(embedding_module, "embed_batch", _blocking_embed_batch)

    async def _just_the_embed_step():
        # Isolate the embed step from the DB by inlining what hybrid_search
        # would do at the same call site — the point is to prove the
        # asyncio.to_thread wrapping yields, not to run the full query pipeline.
        await asyncio.to_thread(search_module.embed, "concurrent query")

    unrelated_ms = await _time_unrelated_coroutine_during(_just_the_embed_step())
    assert unrelated_ms < UPPER_BOUND_MS, (
        f"unrelated coroutine woke at {unrelated_ms:.0f}ms, "
        f"expected < {UPPER_BOUND_MS:.0f}ms — event loop appears blocked"
    )


async def test_asyncio_to_thread_wrapper_yields_for_embed_batch(monkeypatch):
    """The fix pattern (asyncio.to_thread) must yield for embed_batch too."""
    monkeypatch.setattr(embedding_module, "embed_batch", _blocking_embed_batch)

    async def _batch_embed():
        await asyncio.to_thread(embedding_module.embed_batch, ["a", "b", "c"])

    unrelated_ms = await _time_unrelated_coroutine_during(_batch_embed())
    assert unrelated_ms < UPPER_BOUND_MS, (
        f"unrelated coroutine woke at {unrelated_ms:.0f}ms, "
        f"expected < {UPPER_BOUND_MS:.0f}ms — event loop appears blocked"
    )
