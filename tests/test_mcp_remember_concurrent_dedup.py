"""Exact-duplicate detection in MCP `remember` holds under concurrency."""

import asyncio
import json

import pytest

from memory_vault.models.db import execute_query, fetch_all, fetch_one

pytestmark = pytest.mark.asyncio

SPACE = "dedup-race"
TEXT = "same concurrent memory"


async def _space_id() -> int:
    await execute_query(
        "INSERT INTO memory_spaces (name) VALUES (%s) ON CONFLICT (name) DO NOTHING",
        (SPACE,),
        commit=True,
    )
    row = await fetch_one("SELECT id FROM memory_spaces WHERE name = %s", (SPACE,))
    return row["id"]


async def _stored_chunks(space_id: int) -> list[dict]:
    return await fetch_all(
        """SELECT id FROM chunks
           WHERE space_id = %s AND content = %s""",
        (space_id, TEXT),
    )


async def test_concurrent_remember_stores_one_chunk(monkeypatch):
    """Two simultaneous `remember` calls with identical text store one chunk.

    Regression guard for #111. The tool used to SELECT for an existing content
    hash and INSERT in a separate statement, so two callers could both see no
    duplicate and both commit. The barrier below holds each call at the moment
    the old code had finished its duplicate lookup, which is the interleaving
    that made the race deterministic rather than occasional.
    """
    from memory_vault.mcp import server

    space_id = await _space_id()

    # Stub the embedding. This test is about database concurrency, and running
    # the real model on two threads at once crashes the process — the shared
    # SentenceTransformer is not safe for parallel `encode` calls. A fixed
    # vector keeps the test on the behaviour it is actually asserting.
    monkeypatch.setattr(server, "embed", lambda text: [0.1] * 384)

    # Deterministic interleaving: neither call proceeds to its write until both
    # have passed the point where the duplicate check used to happen.
    barrier = asyncio.Barrier(2)
    real_to_thread = asyncio.to_thread

    async def gated_to_thread(fn, *args, **kwargs):
        result = await real_to_thread(fn, *args, **kwargs)
        # Gate only the embed call inside `remember`, and only once per caller.
        # Graph extraction also runs through `to_thread` on the same text, but
        # only the winning caller reaches it — waiting there would block on a
        # partner that already returned.
        if fn is server.embed:
            await barrier.wait()
        return result

    monkeypatch.setattr(server.asyncio, "to_thread", gated_to_thread)

    # Bounded so a barrier that never releases fails the test instead of
    # hanging the suite.
    first, second = await asyncio.wait_for(
        asyncio.gather(
            server.remember(TEXT, space=SPACE),
            server.remember(TEXT, space=SPACE),
        ),
        timeout=60,
    )

    results = [json.loads(first), json.loads(second)]
    stored = [r for r in results if r.get("stored")]
    duplicates = [r for r in results if r.get("duplicate")]

    assert len(stored) == 1, f"expected exactly one store, got {results}"
    assert len(duplicates) == 1, f"expected exactly one duplicate, got {results}"

    rows = await _stored_chunks(space_id)
    assert len(rows) == 1, f"expected one chunk in the database, found {len(rows)}"

    # The losing caller still learns which chunk holds the memory.
    assert duplicates[0]["existing_chunk_id"] == str(rows[0]["id"])


async def test_sequential_duplicate_still_reports_existing_chunk():
    """The ordinary (non-racing) duplicate path keeps its contract.

    The fix moved duplicate detection from a SELECT into ON CONFLICT, so the
    plain case is worth pinning too: same answer, same shape, and no second
    chunk written.
    """
    from memory_vault.mcp import server

    space_id = await _space_id()

    first = json.loads(await server.remember(TEXT, space=SPACE))
    second = json.loads(await server.remember(TEXT, space=SPACE))

    assert first["stored"] is True
    assert second["stored"] is False
    assert second["duplicate"] is True
    assert second["existing_chunk_id"] == first["chunk_id"]

    rows = await _stored_chunks(space_id)
    assert len(rows) == 1


async def test_ingestion_rows_without_content_hash_are_not_constrained():
    """Rows carrying no content hash stay exempt from the unique index.

    File ingestion and `ingest_text` do not persist a content hash, so their
    rows hold NULL at the indexed expression. The index is deliberately
    partial: it must not start rejecting those writes, which would change
    ingestion behaviour well beyond the bug being fixed here.
    """
    space_id = await _space_id()

    for _ in range(2):
        await execute_query(
            """INSERT INTO chunks (space_id, content, metadata)
               VALUES (%s, %s, %s::jsonb)""",
            (space_id, "ingested text", json.dumps({"source_file": "a.md"})),
            commit=True,
        )

    rows = await fetch_all(
        """SELECT id FROM chunks
           WHERE space_id = %s AND content = %s""",
        (space_id, "ingested text"),
    )
    assert len(rows) == 2
