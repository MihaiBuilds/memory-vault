"""Ingestion endpoints — quick text + file upload."""

from __future__ import annotations

import tempfile
from pathlib import Path

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile, status

from src.api.deps import require_token
from src.api.schemas import IngestResponse, IngestTextRequest
from src.models.db import fetch_one
from src.services.ingestion import IngestionPipeline, ingest_text

router = APIRouter(prefix="/api", tags=["ingest"], dependencies=[Depends(require_token)])


async def _resolve_space_id(name: str) -> int:
    row = await fetch_one("SELECT id FROM memory_spaces WHERE name = %s", (name,))
    if not row:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Unknown space: {name}",
        )
    return int(row["id"])


@router.post("/ingest/text", response_model=IngestResponse)
async def ingest_text_endpoint(req: IngestTextRequest) -> IngestResponse:
    """Ingest a single text string as one chunk."""
    await _resolve_space_id(req.space)
    try:
        chunk_id = await ingest_text(
            text=req.text,
            space=req.space,
            source=req.source,
            speaker=req.speaker,
        )
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(e))

    return IngestResponse(
        stored=True,
        chunk_id=chunk_id,
        chunks_created=1,
        message="Text stored successfully.",
    )


@router.post("/ingest/file", response_model=IngestResponse)
async def ingest_file_endpoint(
    file: UploadFile = File(...),
    space: str = Form(default="default"),
) -> IngestResponse:
    """
    Upload a file and run it through the full ingestion pipeline.

    Adapter is auto-detected from filename/content (markdown, plaintext, Claude JSON).
    """
    space_id = await _resolve_space_id(space)

    filename = file.filename or "upload.txt"
    suffix = Path(filename).suffix or ".txt"

    with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
        content = await file.read()
        tmp.write(content)
        tmp_path = tmp.name

    try:
        pipeline = IngestionPipeline(max_workers=1)
        pipeline.enqueue(tmp_path, space_id)
        stats = await pipeline.run_all()
    finally:
        Path(tmp_path).unlink(missing_ok=True)

    if stats.failed:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Ingestion failed: {stats.errors[-1] if stats.errors else 'unknown error'}",
        )

    return IngestResponse(
        stored=stats.chunks_created > 0,
        chunks_created=stats.chunks_created,
        message=f"Ingested {stats.chunks_created} chunks from {filename}",
    )
