"""Memory spaces endpoints — list and create."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status

from memory_vault.api.deps import require_token
from memory_vault.api.schemas import SpaceCreateRequest, SpaceInfo, SpaceList
from memory_vault.models.db import execute_query, fetch_all, fetch_one

router = APIRouter(prefix="/api", tags=["spaces"], dependencies=[Depends(require_token)])

# Names reserved for internal/future use. `default` is also reserved at the
# database level (seeded migration) and would 409 on conflict, but listing it
# here gives a clearer error before we hit the DB.
RESERVED_SPACE_NAMES: frozenset[str] = frozenset(
    {
        "default",
        "system",
        "admin",
        "all",
        "none",
        "_internal",
    }
)


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


@router.post("/spaces", response_model=SpaceInfo, status_code=status.HTTP_201_CREATED)
async def create_space(req: SpaceCreateRequest) -> SpaceInfo:
    if req.name in RESERVED_SPACE_NAMES:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Space name is reserved: {req.name}",
        )

    existing = await fetch_one("SELECT 1 FROM memory_spaces WHERE name = %s", (req.name,))
    if existing:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Space already exists: {req.name}",
        )
    await execute_query(
        "INSERT INTO memory_spaces (name, description) VALUES (%s, %s)",
        (req.name, req.description),
    )
    return SpaceInfo(name=req.name, description=req.description, chunk_count=0)
