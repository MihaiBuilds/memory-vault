"""File ingestion is atomic on failure and idempotent on retry."""

import json

import pytest

from memory_vault.models.db import execute_query, fetch_all, fetch_one
from memory_vault.services.ingestion import IngestionPipeline

pytestmark = pytest.mark.asyncio

SPACE = "ingest-atomicity"


async def _space_id() -> int:
    await execute_query(
        "INSERT INTO memory_spaces (name) VALUES (%s) ON CONFLICT (name) DO NOTHING",
        (SPACE,),
        commit=True,
    )
    row = await fetch_one("SELECT id FROM memory_spaces WHERE name = %s", (SPACE,))
    return row["id"]


async def _chunks_for(space_id: int, source_file: str) -> list[dict]:
    return await fetch_all(
        """SELECT id, content, chunk_index FROM chunks
           WHERE space_id = %s AND metadata->>'source_file' = %s
           ORDER BY chunk_index""",
        (space_id, source_file),
    )


def _write(tmp_path, name: str, body: str) -> str:
    path = tmp_path / name
    path.write_text(body, encoding="utf-8")
    return str(path)


@pytest.fixture(autouse=True)
def _stub_embedding(monkeypatch):
    """Keep the real model out of these tests.

    They exercise transaction and conflict behaviour, not embedding quality,
    and the shared model cannot be driven from several threads safely (#148).
    """
    import memory_vault.services.ingestion as ing

    monkeypatch.setattr(ing, "embed_batch", lambda texts, **kw: [[0.1] * 384 for _ in texts])


async def test_failed_file_leaves_no_partial_chunks(tmp_path, monkeypatch):
    """A failure partway through a file commits nothing.

    Regression guard for #115. Each chunk used to be committed on its own, so
    a file that failed on its third chunk left the first two behind with
    nothing recording that the file was incomplete.
    """
    import memory_vault.services.ingestion as ing

    space_id = await _space_id()
    path = _write(tmp_path, "partial.md", "# A\n\nfirst\n\n# B\n\nsecond\n\n# C\n\nthird\n")

    pipeline = IngestionPipeline()
    real_insert = ing.IngestionPipeline._insert_chunk
    calls = {"n": 0}

    # Wrapped with *args so the injection does not depend on the insert
    # helper's signature — otherwise this test can fail for the wrong reason
    # when run against a different arrangement of the code.
    async def failing_insert(self, *args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 3:
            raise RuntimeError("injected failure on the third chunk")
        return await real_insert(self, *args, **kwargs)

    monkeypatch.setattr(ing.IngestionPipeline, "_insert_chunk", failing_insert)

    pipeline.enqueue(path, space_id)
    stats = await pipeline.run_all()

    assert stats.failed == 1, "the file should be reported as failed"
    assert calls["n"] == 3, "the failure should fire on the third insert"

    rows = await _chunks_for(space_id, path)
    assert rows == [], f"failed file left {len(rows)} chunk(s) behind"


async def test_retry_after_failure_produces_no_duplicates(tmp_path, monkeypatch):
    """Re-ingesting a file that failed once stores each chunk exactly once."""
    import memory_vault.services.ingestion as ing

    space_id = await _space_id()
    path = _write(tmp_path, "retry.md", "# A\n\nalpha\n\n# B\n\nbeta\n")

    real_insert = ing.IngestionPipeline._insert_chunk
    calls = {"n": 0}

    async def failing_insert(self, *args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 2:
            raise RuntimeError("injected failure")
        return await real_insert(self, *args, **kwargs)

    monkeypatch.setattr(ing.IngestionPipeline, "_insert_chunk", failing_insert)
    first = IngestionPipeline()
    first.enqueue(path, space_id)
    assert (await first.run_all()).failed == 1

    # Retry with the failure removed.
    monkeypatch.setattr(ing.IngestionPipeline, "_insert_chunk", real_insert)
    second = IngestionPipeline()
    second.enqueue(path, space_id)
    assert (await second.run_all()).failed == 0

    rows = await _chunks_for(space_id, path)
    contents = [r["content"] for r in rows]
    assert len(rows) == 2, f"expected 2 chunks after retry, found {len(rows)}: {contents}"
    assert len(set(contents)) == 2, f"retry duplicated content: {contents}"


async def test_reingesting_an_unchanged_file_is_a_no_op(tmp_path):
    """Ingesting the same file twice does not double its chunks."""
    space_id = await _space_id()
    path = _write(tmp_path, "stable.md", "# A\n\nalpha\n\n# B\n\nbeta\n")

    for _ in range(2):
        pipeline = IngestionPipeline()
        pipeline.enqueue(path, space_id)
        assert (await pipeline.run_all()).failed == 0

    rows = await _chunks_for(space_id, path)
    assert len(rows) == 2, f"second ingest added chunks: found {len(rows)}"


async def test_repeated_passages_in_one_file_are_all_stored(tmp_path):
    """Two identical passages in one file both survive ingestion.

    Keying idempotency on the content hash alone would treat the second copy
    as a duplicate and silently drop it. Repeated text is ordinary in
    changelogs, logs, and transcripts, so the chunk's position is part of its
    identity.
    """
    space_id = await _space_id()
    body = "# v1.1\n\nFixed the connection pool bug.\n\n# v1.0\n\nFixed the connection pool bug.\n"
    path = _write(tmp_path, "changelog.md", body)

    pipeline = IngestionPipeline()
    pipeline.enqueue(path, space_id)
    assert (await pipeline.run_all()).failed == 0

    rows = await _chunks_for(space_id, path)
    assert len(rows) == 2, f"a legitimately repeated passage was dropped: {rows}"
    assert rows[0]["content"] == rows[1]["content"]
    assert rows[0]["chunk_index"] != rows[1]["chunk_index"]


async def test_ingested_chunks_carry_identity_metadata(tmp_path):
    """Ingestion persists the markers the retry guarantee depends on."""
    space_id = await _space_id()
    path = _write(tmp_path, "meta.md", "# A\n\nalpha\n")

    pipeline = IngestionPipeline()
    pipeline.enqueue(path, space_id)
    assert (await pipeline.run_all()).failed == 0

    row = await fetch_one(
        "SELECT metadata FROM chunks WHERE space_id = %s AND metadata->>'source_file' = %s",
        (space_id, path),
    )
    metadata = row["metadata"]
    if isinstance(metadata, str):
        metadata = json.loads(metadata)

    assert metadata["source_file"] == path
    assert metadata["content_hash"], "content_hash must be persisted for retry idempotency"
