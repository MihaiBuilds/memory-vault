"""Memory spaces endpoint — list only (creation is manual via migration/seed)."""

from __future__ import annotations

from fastapi import APIRouter, Depends

from src.api.deps import require_token
from src.api.schemas import SpaceInfo, SpaceList
from src.models.db import fetch_all

router = APIRouter(prefix="/api", tags=["spaces"], dependencies=[Depends(require_token)])


@router.get("/spaces", response_model=SpaceList)
async def list_spaces() -> SpaceList:
    rows = await fetch_all(
        """SELECT ms.name, ms.description,
                  COUNT(c.id) FILTER (
                      WHERE c.importance > 0
                        AND (c.metadata->>'forgotten')::boolean IS NOT TRUE
                  ) AS chunk_count
           FROM memory_spaces ms
           LEFT JOIN chunks c ON c.space_id = ms.id
           GROUP BY ms.id, ms.name, ms.description
           ORDER BY ms.name"""
    )
    return SpaceList(
        spaces=[
            SpaceInfo(
                name=r["name"],
                description=r["description"],
                chunk_count=int(r["chunk_count"]),
            )
            for r in rows
        ]
    )
