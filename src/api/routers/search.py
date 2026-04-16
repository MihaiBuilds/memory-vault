"""Hybrid search endpoint."""

from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, status

from src.api.deps import require_token
from src.api.schemas import SearchHit, SearchRequest, SearchResponse
from src.services.search import hybrid_search, log_query, resolve_space_names

router = APIRouter(prefix="/api", tags=["search"], dependencies=[Depends(require_token)])


@router.post("/search", response_model=SearchResponse)
async def search(req: SearchRequest) -> SearchResponse:
    """Run hybrid search (vector + full-text + RRF) and return ranked hits."""
    space_ids = await resolve_space_names(req.spaces) if req.spaces else None

    since_dt: datetime | None = None
    if req.since:
        try:
            since_dt = datetime.fromisoformat(req.since).replace(tzinfo=timezone.utc)
        except ValueError:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Invalid date format: {req.since}. Use ISO format (YYYY-MM-DD).",
            )

    results, variations, elapsed_ms = await hybrid_search(
        query_text=req.query,
        space_ids=space_ids or None,
        since=since_dt,
        limit=req.limit,
    )

    await log_query(req.query, space_ids or None, results, elapsed_ms)

    hits = [
        SearchHit(
            chunk_id=r.chunk_id,
            content=r.content,
            similarity=r.similarity,
            space=r.space,
            speaker=r.speaker,
            source=r.source,
            created_at=r.created_at,
            metadata=r.metadata,
        )
        for r in results
    ]

    return SearchResponse(
        results=hits,
        total_results=len(hits),
        query_variations=variations,
        query_time_ms=elapsed_ms,
    )
