"""Hybrid search endpoint."""

from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, status

from memory_vault.api.deps import require_token
from memory_vault.api.schemas import (
    SearchHit,
    SearchQualityResponse,
    SearchRequest,
    SearchResponse,
)
from memory_vault.services.search import (
    SEARCH_QUALITY_WINDOW_HOURS,
    WEAK_MATCH_SIMILARITY,
    hybrid_search,
    log_query,
    parse_since,
    recent_search_quality,
    resolve_space_names,
)

router = APIRouter(prefix="/api", tags=["search"], dependencies=[Depends(require_token)])


@router.post("/search", response_model=SearchResponse)
async def search(req: SearchRequest) -> SearchResponse:
    """Run hybrid search (vector + full-text + RRF) and return ranked hits."""
    space_ids = await resolve_space_names(req.spaces) if req.spaces else None

    since_dt: datetime | None = None
    if req.since:
        try:
            since_dt = parse_since(req.since)
        except ValueError as e:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Invalid date format: {req.since}. Use ISO format (YYYY-MM-DD).",
            ) from e

    results, variations, elapsed_ms = await hybrid_search(
        query_text=req.query,
        space_ids=space_ids,
        since=since_dt,
        limit=req.limit,
        ef_search=req.ef_search,
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


@router.get("/search/quality", response_model=SearchQualityResponse)
async def search_quality() -> SearchQualityResponse:
    """Report how well recent searches have been matching.

    Read-only over what searching already recorded — it runs no query of its
    own, so asking does not change the answer.
    """
    quality = await recent_search_quality()

    return SearchQualityResponse(
        queries=quality.queries,
        window_hours=SEARCH_QUALITY_WINDOW_HOURS,
        avg_top_similarity=quality.avg_top_similarity,
        weak_matches=quality.weak_matches,
        empty_results=quality.empty_results,
        weak_threshold=WEAK_MATCH_SIMILARITY,
    )
