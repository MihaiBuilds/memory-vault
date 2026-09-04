"""Memory spaces endpoints — list, create, and delete."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Path, status

from memory_vault.api.deps import require_token
from memory_vault.api.schemas import SpaceCreateRequest, SpaceInfo, SpaceList
from memory_vault.models.db import execute_returning, fetch_all
from memory_vault.services.spaces import (
    RESERVED_SPACE_NAMES,
    SpaceNotEmpty,
    SpaceNotFound,
    SpaceReserved,
    delete_space,
)

router = APIRouter(prefix="/api", tags=["spaces"], dependencies=[Depends(require_token)])

__all__ = ["RESERVED_SPACE_NAMES", "router"]


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

    # Let the insert decide, rather than checking first and inserting after.
    # A separate SELECT leaves a window where two callers both see no row and
    # both try to insert: the loser used to hit the unique constraint and
    # surface as a 500. Here the conflict is expected, so the loser simply
    # gets no row back and is answered with the same 409 a non-racing
    # duplicate receives.
    row = await execute_returning(
        """INSERT INTO memory_spaces (name, description) VALUES (%s, %s)
           ON CONFLICT (name) DO NOTHING
           RETURNING id""",
        (req.name, req.description),
    )
    if row is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Space already exists: {req.name}",
        )
    return SpaceInfo(name=req.name, description=req.description, chunk_count=0)


@router.delete("/spaces/{name}", status_code=status.HTTP_204_NO_CONTENT)
async def remove_space(
    name: str = Path(
        ...,
        min_length=1,
        max_length=64,
        pattern=r"^[a-z0-9][a-z0-9-]*$",
        description="Name of the space to delete. It must hold no memories or entities.",
    ),
) -> None:
    """Delete an empty space.

    Deliberately refuses to delete anything but an empty space: 409 rather
    than removing someone's memories because they asked to tidy up a name.
    See `delete_space` for why emptiness covers entities as well as chunks.
    """
    try:
        await delete_space(name)
    except SpaceReserved as e:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(e)) from e
    except SpaceNotFound as e:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(e)) from e
    except SpaceNotEmpty as e:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(e)) from e
