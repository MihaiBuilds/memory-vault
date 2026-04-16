"""Chunk management — list, fetch, soft-delete."""

from __future__ import annotations

import json
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, status

from src.api.deps import require_token
from src.api.schemas import ChunkList, ChunkSummary, ForgetResponse
from src.models.db import execute_query, fetch_all, fetch_one

router = APIRouter(prefix="/api", tags=["chunks"], dependencies=[Depends(require_token)])


def _row_to_summary(row: dict) -> ChunkSummary:
    meta = row.get("metadata") or {}
    if isinstance(meta, str):
        try:
            meta = json.loads(meta)
        except (json.JSONDecodeError, TypeError):
            meta = {}
    return ChunkSummary(
        chunk_id=str(row["id"]),
        content=row["content"],
        space=row["space"],
        source=row.get("source"),
        speaker=row.get("speaker"),
        importance=float(row.get("importance") or 0.0),
        created_at=row.get("created_at"),
        metadata=meta,
    )


@router.get("/chunks", response_model=ChunkList)
async def list_chunks(
    space: str | None = Query(default=None, description="Filter by space name"),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    sort: str = Query(default="recent", pattern="^(recent|importance)$"),
    include_forgotten: bool = Query(default=False),
) -> ChunkList:
    """List chunks with pagination. Defaults to newest first, active only."""
    where: list[str] = []
    params: list = []

    if not include_forgotten:
        where.append("(c.metadata->>'forgotten')::boolean IS NOT TRUE")

    if space:
        where.append("ms.name = %s")
        params.append(space)

    where_sql = "WHERE " + " AND ".join(where) if where else ""
    order_sql = "ORDER BY c.created_at DESC" if sort == "recent" else "ORDER BY c.importance DESC, c.created_at DESC"

    total_row = await fetch_one(
        f"""SELECT COUNT(*) AS n
            FROM chunks c JOIN memory_spaces ms ON ms.id = c.space_id
            {where_sql}""",
        tuple(params) if params else None,
    )
    total = int(total_row["n"]) if total_row else 0

    rows = await fetch_all(
        f"""SELECT c.id, c.content, c.source, c.speaker, c.importance,
                   c.created_at, c.metadata, ms.name AS space
            FROM chunks c JOIN memory_spaces ms ON ms.id = c.space_id
            {where_sql}
            {order_sql}
            LIMIT %s OFFSET %s""",
        tuple([*params, limit, offset]),
    )

    return ChunkList(
        chunks=[_row_to_summary(r) for r in rows],
        total=total,
        limit=limit,
        offset=offset,
    )


@router.get("/chunks/{chunk_id}", response_model=ChunkSummary)
async def get_chunk(chunk_id: str) -> ChunkSummary:
    row = await fetch_one(
        """SELECT c.id, c.content, c.source, c.speaker, c.importance,
                  c.created_at, c.metadata, ms.name AS space
           FROM chunks c JOIN memory_spaces ms ON ms.id = c.space_id
           WHERE c.id = %s""",
        (chunk_id,),
    )
    if not row:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Chunk not found")
    return _row_to_summary(row)


@router.delete("/chunks/{chunk_id}", response_model=ForgetResponse)
async def forget_chunk(chunk_id: str) -> ForgetResponse:
    """Soft-delete a chunk (same behavior as the MCP `forget` tool)."""
    row = await fetch_one(
        "SELECT id, content, metadata FROM chunks WHERE id = %s",
        (chunk_id,),
    )
    if not row:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Chunk not found")

    meta = row["metadata"] or {}
    if isinstance(meta, str):
        meta = json.loads(meta)

    if meta.get("forgotten"):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Chunk is already forgotten",
        )

    meta["forgotten"] = True
    meta["forgotten_at"] = datetime.now(timezone.utc).isoformat()

    await execute_query(
        """UPDATE chunks
           SET importance = 0,
               metadata = %s::jsonb,
               updated_at = now()
           WHERE id = %s""",
        (json.dumps(meta), chunk_id),
    )

    preview = row["content"][:80] + ("..." if len(row["content"]) > 80 else "")
    return ForgetResponse(
        success=True,
        chunk_id=chunk_id,
        message=f'Memory forgotten: "{preview}"',
    )
