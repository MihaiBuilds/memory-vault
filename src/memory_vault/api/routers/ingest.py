"""Ingestion endpoints — quick text + file upload."""

from __future__ import annotations

import logging
import tempfile
from pathlib import Path

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile, status

from memory_vault.api.deps import require_token
from memory_vault.api.schemas import (
    IngestFileResult,
    IngestFilesResponse,
    IngestResponse,
    IngestTextRequest,
)
from memory_vault.services.ingestion import IngestionPipeline, ingest_text
from memory_vault.services.spaces import InvalidSpaceName, ensure_space

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api", tags=["ingest"], dependencies=[Depends(require_token)])

# 25 MB cap on uploads — generous for personal-memory ingestion (a single
# markdown export, conversation log, or transcript), small enough to keep
# tempfile + pipeline memory bounded.
MAX_UPLOAD_BYTES = 25 * 1024 * 1024
_UPLOAD_CHUNK = 1024 * 1024  # 1 MB streaming reads

# Batch limits. The per-file cap is the single-upload cap, unchanged — one
# file should not become more permissive by arriving with company. The
# aggregate cap is what stops fifty files of 24 MB each, and the file-count
# cap stops ten thousand tiny ones, which is a different way to spend the
# same afternoon.
MAX_BATCH_FILES = 100
MAX_BATCH_BYTES = 100 * 1024 * 1024

# What a file is rejected for before anything is read. Kept as a message the
# caller can act on, rather than the underlying exception.
_EMPTY_FILE = "File is empty."
_TOO_LARGE = f"File exceeds the {MAX_UPLOAD_BYTES // (1024 * 1024)} MB per-file limit."
_BATCH_FULL = f"Skipped: the batch reached its {MAX_BATCH_BYTES // (1024 * 1024)} MB total limit."


def _reject_bad_filename(filename: str) -> str | None:
    """Return why `filename` is unusable, or None if it is fine.

    Same rule as the single-file upload: the on-disk path is a tempfile we
    control, so there is no real escape, but the name is echoed back and
    stored as the chunk's source, and "../../etc/passwd" should not turn up
    in the dashboard.
    """
    if not filename:
        return "Missing filename."
    if ".." in filename or "/" in filename or "\\" in filename or filename.startswith("."):
        return "Invalid filename. Path separators and traversal patterns are not allowed."
    return None


async def _resolve_space_id(name: str) -> int:
    """Return the space's id, creating it on first write.

    Ingesting into a space that does not exist yet used to 404, which meant a
    caller had to create the space in a separate request before its first
    write. The name still has to be one the API would accept from an explicit
    create, so a typo cannot quietly produce a junk space with any spelling.
    """
    try:
        return await ensure_space(name)
    except InvalidSpaceName as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e)) from e


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
                        status_code=status.HTTP_413_CONTENT_TOO_LARGE,
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
            # Read from the tempfile, but record the name the user uploaded.
            # The tempfile is deleted when this request ends, so persisting its
            # path left every uploaded chunk pointing at something gone.
            pipeline.enqueue(tmp_path, space_id, source_name=filename)
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


async def _spool_upload(
    file: UploadFile, remaining_budget: int
) -> tuple[str | None, int, str | None]:
    """Stream one upload to a tempfile.

    Returns (temp path, bytes written, rejection reason). Exactly one of the
    path or the reason is set. Streamed rather than read whole so a large
    upload is rejected while it arrives instead of after it has been held in
    memory — and in a batch that matters more, since several could arrive at
    once.
    """
    filename = file.filename or ""
    suffix = Path(filename).suffix or ".txt"

    written = 0
    tmp_path: str | None = None
    try:
        with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
            tmp_path = tmp.name
            while True:
                blob = await file.read(_UPLOAD_CHUNK)
                if not blob:
                    break
                written += len(blob)
                if written > MAX_UPLOAD_BYTES:
                    Path(tmp_path).unlink(missing_ok=True)
                    return None, 0, _TOO_LARGE
                if written > remaining_budget:
                    Path(tmp_path).unlink(missing_ok=True)
                    return None, 0, _BATCH_FULL
                tmp.write(blob)
    except OSError:
        if tmp_path:
            Path(tmp_path).unlink(missing_ok=True)
        logger.exception("Failed to spool upload %s", filename)
        return None, 0, "Could not read the uploaded file."

    if written == 0:
        Path(tmp_path).unlink(missing_ok=True)
        return None, 0, _EMPTY_FILE

    return tmp_path, written, None


@router.post("/ingest/files", response_model=IngestFilesResponse)
async def ingest_files_endpoint(
    files: list[UploadFile] = File(...),
    space: str = Form(default="default"),
) -> IngestFilesResponse:
    """Upload several files in one request.

    Answers per file rather than as a single verdict. One malformed file in a
    batch of thirty should not discard the other twenty-nine, and the caller
    needs to know which one to fix — so the response lists every file with its
    own outcome, and the request succeeds as long as it was well-formed.

    A batch is bounded three ways: per file (the same cap a lone upload gets),
    in aggregate, and by file count. Any one of them alone leaves an easy way
    to spend all the disk and time available.
    """
    if len(files) > MAX_BATCH_FILES:
        raise HTTPException(
            status_code=status.HTTP_413_CONTENT_TOO_LARGE,
            detail=f"Too many files. Maximum is {MAX_BATCH_FILES} per request.",
        )

    space_id = await _resolve_space_id(space)

    results: list[IngestFileResult] = []
    # Tempfile path -> the name the user uploaded. The pipeline reports errors
    # keyed by the path it was given, and that path is a tempfile nobody asked
    # about; echoing it back would leak server filesystem layout and mean
    # nothing to the caller. This maps it back to their filename.
    spooled: dict[str, str] = {}
    budget = MAX_BATCH_BYTES

    try:
        for upload in files:
            filename = upload.filename or ""

            reason = _reject_bad_filename(filename)
            if reason:
                results.append(
                    IngestFileResult(filename=filename or "(unnamed)", stored=False, error=reason)
                )
                continue

            tmp_path, written, reason = await _spool_upload(upload, budget)
            if reason:
                results.append(IngestFileResult(filename=filename, stored=False, error=reason))
                continue

            assert tmp_path is not None  # nosec B101 — guaranteed when reason is None
            budget -= written
            spooled[tmp_path] = filename

        chunks_created = 0
        failures_by_name: dict[str, str] = {}

        if spooled:
            pipeline = IngestionPipeline(max_workers=1)
            for tmp_path, filename in spooled.items():
                # source_name is what the chunk records. Without it every
                # chunk would point at a tempfile that is deleted before the
                # response is sent — the same defect as a single upload, one
                # per file instead of one per request.
                pipeline.enqueue(tmp_path, space_id, source_name=filename)

            try:
                stats = await pipeline.run_all()
            except Exception as exc:
                logger.exception("Batch ingestion crashed for %d files", len(spooled))
                raise HTTPException(
                    status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                    detail="Ingestion failed. Check server logs.",
                ) from exc

            chunks_created = stats.chunks_created

            for raw in stats.errors:
                # Errors arrive as "<path>: <message>". Match the path back to
                # the filename and keep only the message.
                path, _, message = raw.partition(": ")
                name = spooled.get(path)
                if name is not None:
                    failures_by_name[name] = message or "Could not be ingested."
                else:
                    logger.warning("Unmatched batch ingestion error: %s", raw)

            for filename in spooled.values():
                if filename in failures_by_name:
                    results.append(
                        IngestFileResult(
                            filename=filename, stored=False, error=failures_by_name[filename]
                        )
                    )
                else:
                    results.append(IngestFileResult(filename=filename, stored=True))

        succeeded = sum(1 for r in results if r.stored)
        failed = len(results) - succeeded

        return IngestFilesResponse(
            files=results,
            files_succeeded=succeeded,
            files_failed=failed,
            chunks_created=chunks_created,
            message=(
                f"Ingested {chunks_created} chunks from {succeeded} "
                f"file{'' if succeeded == 1 else 's'}" + (f"; {failed} failed" if failed else "")
            ),
        )

    finally:
        for tmp_path in spooled:
            Path(tmp_path).unlink(missing_ok=True)
