"""
Ingestion pipeline — async queue that processes files into chunks.

Pipeline per file: read → detect adapter → parse → batch embed → insert.
Uses asyncio.PriorityQueue with configurable concurrency.
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from dataclasses import dataclass, field
from enum import IntEnum
from pathlib import Path

from src.adapters.base import RawChunk, detect_adapter
from src.extraction import (
    extract_entities,
    extract_relationships,
    write_graph_for_chunk,
)
from src.models.db import execute_query, fetch_one, get_pool
from src.services.embedding import embed_batch

logger = logging.getLogger(__name__)

MAX_WORKERS = 5


class Priority(IntEnum):
    REALTIME = 0
    HIGH = 1
    BATCH = 2


@dataclass
class IngestionStats:
    queued: int = 0
    active: int = 0
    completed: int = 0
    failed: int = 0
    chunks_created: int = 0
    errors: list[str] = field(default_factory=list)


@dataclass(order=True)
class IngestionJob:
    priority: int
    file_path: str = field(compare=False)
    space_id: int = field(compare=False)


class IngestionPipeline:
    """Async pipeline that ingests files into the vector database."""

    def __init__(self, max_workers: int = MAX_WORKERS) -> None:
        self._queue: asyncio.PriorityQueue[IngestionJob] = asyncio.PriorityQueue()
        self._max_workers = max_workers
        self._workers: list[asyncio.Task] = []
        self._stats = IngestionStats()
        self._running = False

    @property
    def stats(self) -> IngestionStats:
        return self._stats

    def enqueue(
        self,
        file_path: str,
        space_id: int,
        priority: Priority = Priority.BATCH,
    ) -> None:
        job = IngestionJob(priority=priority, file_path=file_path, space_id=space_id)
        self._queue.put_nowait(job)
        self._stats.queued += 1

    async def start(self) -> None:
        self._running = True
        for i in range(self._max_workers):
            task = asyncio.create_task(self._worker(i))
            self._workers.append(task)

    async def drain(self) -> IngestionStats:
        await self._queue.join()
        self._running = False
        for task in self._workers:
            task.cancel()
        await asyncio.gather(*self._workers, return_exceptions=True)
        self._workers.clear()
        return self._stats

    async def run_all(self) -> IngestionStats:
        await self.start()
        return await self.drain()

    async def _worker(self, worker_id: int) -> None:
        while self._running:
            try:
                job = await asyncio.wait_for(self._queue.get(), timeout=1.0)
            except asyncio.TimeoutError:
                continue
            except asyncio.CancelledError:
                break

            self._stats.active += 1
            self._stats.queued -= 1

            try:
                await self._process_file(job)
                self._stats.completed += 1
            except Exception as e:
                self._stats.failed += 1
                self._stats.errors.append(f"{job.file_path}: {e}")
                logger.exception("Worker %d failed on %s", worker_id, job.file_path)
            finally:
                self._stats.active -= 1
                self._queue.task_done()

    async def _process_file(self, job: IngestionJob) -> None:
        file_path = job.file_path
        space_id = job.space_id

        raw = Path(file_path).read_text(encoding="utf-8", errors="replace")
        if not raw.strip():
            logger.warning("Skipping empty file: %s", file_path)
            return

        adapter = detect_adapter(file_path, content=raw)
        raw_chunks = adapter.parse(raw, source_path=file_path)

        if not raw_chunks:
            logger.warning("No chunks produced from: %s", file_path)
            return

        # Batch embed
        texts = [c.text for c in raw_chunks]
        embeddings = embed_batch(texts)

        # Insert each chunk
        for chunk, emb in zip(raw_chunks, embeddings):
            await self._insert_chunk(chunk, emb, space_id)

    async def _insert_chunk(
        self,
        chunk: RawChunk,
        embedding: list[float],
        space_id: int,
    ) -> None:
        chunk_id = str(uuid.uuid4())
        meta_json = json.dumps(chunk.metadata, default=str)

        await execute_query(
            """INSERT INTO chunks
                   (id, space_id, content, embedding, source, speaker,
                    metadata, chunk_index, created_at)
               VALUES (%s, %s, %s, %s::vector, %s, %s, %s::jsonb, %s, COALESCE(%s, now()))""",
            (
                chunk_id,
                space_id,
                chunk.text,
                str(embedding),
                chunk.metadata.get("source_file", ""),
                chunk.speaker,
                meta_json,
                chunk.chunk_index,
                chunk.timestamp,
            ),
        )
        self._stats.chunks_created += 1

        await _run_extraction(chunk_id, chunk.text, space_id)


async def ingest_text(
    text: str,
    space: str = "default",
    source: str = "api",
    speaker: str | None = None,
) -> str:
    """Quick-ingest a single text string. Returns the chunk ID."""
    from src.services.embedding import embed

    # Resolve space name to ID
    row = await fetch_one(
        "SELECT id FROM memory_spaces WHERE name = %s", (space,)
    )
    if not row:
        raise ValueError(f"Unknown space: {space}")

    space_id = row["id"]
    chunk_id = str(uuid.uuid4())
    embedding = embed(text)

    await execute_query(
        """INSERT INTO chunks (id, space_id, content, embedding, source, speaker, chunk_index)
           VALUES (%s, %s, %s, %s::vector, %s, %s, 0)""",
        (chunk_id, space_id, text, str(embedding), source, speaker),
    )

    await _run_extraction(chunk_id, text, space_id)
    return chunk_id


async def _run_extraction(chunk_id: str, text: str, space_id: int) -> None:
    """Run spaCy extraction in a thread, then persist the graph writes.

    Swallows exceptions — the chunk is already committed; extraction is
    best-effort. Graph-writer handles its own transaction boundary and
    logs internally on failure.
    """
    try:
        entities = await asyncio.to_thread(extract_entities, text)
        relationships = await asyncio.to_thread(
            extract_relationships, entities, text
        )
    except Exception:
        logger.exception(
            "Extraction failed for chunk %s — graph data absent, chunk retained",
            chunk_id,
        )
        return

    await write_graph_for_chunk(chunk_id, space_id, entities, relationships)
