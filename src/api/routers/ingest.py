"""Ingestion endpoints — quick text + file upload."""

from __future__ import annotations

import logging
import tempfile
from pathlib import Path

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile, status

from src.api.deps import require_token
from src.api.schemas import IngestResponse, IngestTextRequest
from src.models.db import fetch_one
from src.services.ingestion import IngestionPipeline, ingest_text

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api", tags=["ingest"], dependencies=[Depends(require_token)])

# 25 MB cap on uploads — generous for personal-memory ingestion (a single
# markdown export, conversation log, or transcript), small enough to keep
# tempfile + pipeline memory bounded.
MAX_UPLOAD_BYTES = 25 * 1024 * 1024
_UPLOAD_CHUNK = 1024 * 1024  # 1 MB streaming reads


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
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e)) from e

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

    # Reject path-traversal patterns and absolute/multi-segment paths in the
    # uploaded filename. The actual on-disk path is a tempfile we control, so
    # there's no real escape — but the filename is echoed in the response and
    # stored in the chunk's source metadata, so we do not want "../../etc/passwd"
    # surfacing in the dashboard or exports.
    if ".." in filename or "/" in filename or "\\" in filename or filename.startswith("."):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid filename. Path separators and traversal patterns are not allowed.",
        )

    suffix = Path(filename).suffix or ".txt"

    # Stream the upload to a tempfile while enforcing the size cap. Reading
    # the whole upload into memory first would let a malicious or accidental
    # 1GB upload exhaust the process before we can reject it.
    bytes_written = 0
    tmp_path: str | None = None
    try:
        with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
            tmp_path = tmp.name
            while True:
                chunk = await file.read(_UPLOAD_CHUNK)
                if not chunk:
                    break
                bytes_written += len(chunk)
                if bytes_written > MAX_UPLOAD_BYTES:
                    raise HTTPException(
                        status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                        detail=(
                            f"File too large. Maximum upload size is "
                            f"{MAX_UPLOAD_BYTES // (1024 * 1024)} MB."
                        ),
                    )
                tmp.write(chunk)

        if bytes_written == 0:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Uploaded file is empty.",
            )

        try:
            pipeline = IngestionPipeline(max_workers=1)
            pipeline.enqueue(tmp_path, space_id)
            stats = await pipeline.run_all()
        except HTTPException:
            raise
        except Exception as exc:
            # Don't leak the underlying exception message to the client —
            # it can include filesystem paths from temp upload handling.
            # Full traceback goes to logs, X-Request-ID lets users correlate.
            logger.exception("Ingestion pipeline crashed for %s", filename)
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Ingestion failed. Check server logs.",
            ) from exc

        if stats.failed:
            # Adapter-level failure (bad file content, unsupported format) is
            # the user's bad input, not a server fault — return 400.
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=(
                    f"Could not ingest {filename}: "
                    f"{stats.errors[-1] if stats.errors else 'unknown adapter error'}"
                ),
            )

        return IngestResponse(
            stored=stats.chunks_created > 0,
            chunks_created=stats.chunks_created,
            message=f"Ingested {stats.chunks_created} chunks from {filename}",
        )

    finally:
        if tmp_path:
            Path(tmp_path).unlink(missing_ok=True)
