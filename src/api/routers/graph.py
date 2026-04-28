"""Knowledge graph endpoints — entities, relationships, and visualization."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, status

from src.api.deps import require_token
from src.api.schemas import (
    EntityDetail,
    EntityList,
    EntityMention,
    EntitySummary,
    GraphEdge,
    GraphNode,
    GraphVisualization,
    RelatedEntity,
    RelationshipList,
    RelationshipRow,
)
from src.models.db import fetch_all, fetch_one

router = APIRouter(prefix="/api/graph", tags=["graph"], dependencies=[Depends(require_token)])

CHUNK_PREVIEW_LEN = 200


# ---------------------------------------------------------------------------
# /entities  —  paginated list
# ---------------------------------------------------------------------------


@router.get("/entities", response_model=EntityList)
async def list_entities(
    space: str | None = Query(default=None, description="Filter by space name."),
    type: str | None = Query(default=None, description="Filter by entity type."),
    min_mentions: int = Query(default=1, ge=1),
    limit: int = Query(default=50, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
) -> EntityList:
    where: list[str] = []
    params: list = []

    if space is not None:
        where.append("ms.name = %s")
        params.append(space)
    if type is not None:
        where.append("e.type = %s")
        params.append(type)

    where_sql = " AND ".join(where) if where else "TRUE"

    # Subquery builds entity + mention_count, then filters by min_mentions.
    base_sql = f"""
        SELECT e.id, e.name, e.type, ms.name AS space_name, e.created_at,
               COUNT(em.id) AS mention_count
        FROM entities e
        JOIN memory_spaces ms ON ms.id = e.space_id
        LEFT JOIN entity_mentions em ON em.entity_id = e.id
        WHERE {where_sql}
        GROUP BY e.id, ms.name
        HAVING COUNT(em.id) >= %s
    """

    count_sql = f"SELECT COUNT(*) AS total FROM ({base_sql}) sub"
    rows_sql = base_sql + " ORDER BY mention_count DESC, e.name ASC LIMIT %s OFFSET %s"

    count_row = await fetch_one(count_sql, tuple(params + [min_mentions]))
    total = int(count_row["total"]) if count_row else 0

    rows = await fetch_all(rows_sql, tuple(params + [min_mentions, limit, offset]))

    entities = [
        EntitySummary(
            id=str(r["id"]),
            name=r["name"],
            type=r["type"],
            space=r["space_name"],
            mention_count=int(r["mention_count"]),
            created_at=r["created_at"],
        )
        for r in rows
    ]

    return EntityList(entities=entities, total=total, limit=limit, offset=offset)


# ---------------------------------------------------------------------------
# /entities/{id}  —  detail + mentions + related
# ---------------------------------------------------------------------------


@router.get("/entities/{entity_id}", response_model=EntityDetail)
async def get_entity(entity_id: str) -> EntityDetail:
    entity = await fetch_one(
        """SELECT e.id, e.name, e.type, ms.name AS space_name, e.created_at,
                  (SELECT COUNT(*) FROM entity_mentions em WHERE em.entity_id = e.id)
                      AS mention_count
           FROM entities e
           JOIN memory_spaces ms ON ms.id = e.space_id
           WHERE e.id = %s""",
        (entity_id,),
    )
    if not entity:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Entity not found: {entity_id}",
        )

    # All mentions with a short chunk preview, newest first.
    mention_rows = await fetch_all(
        """SELECT em.chunk_id, em.start_offset, em.end_offset,
                  LEFT(c.content, %s) AS chunk_preview
           FROM entity_mentions em
           JOIN chunks c ON c.id = em.chunk_id
           WHERE em.entity_id = %s
           ORDER BY em.created_at DESC""",
        (CHUNK_PREVIEW_LEN, entity_id),
    )

    # Related entities via relationships (both directions), aggregated.
    related_rows = await fetch_all(
        """SELECT other.id, other.name, other.type, COUNT(*) AS co_mention_count
           FROM relationships r
           JOIN entities other ON other.id = CASE
               WHEN r.source_entity_id = %s THEN r.target_entity_id
               ELSE r.source_entity_id
           END
           WHERE r.source_entity_id = %s OR r.target_entity_id = %s
           GROUP BY other.id, other.name, other.type
           ORDER BY co_mention_count DESC, other.name ASC""",
        (entity_id, entity_id, entity_id),
    )

    return EntityDetail(
        id=str(entity["id"]),
        name=entity["name"],
        type=entity["type"],
        space=entity["space_name"],
        mention_count=int(entity["mention_count"]),
        created_at=entity["created_at"],
        mentions=[
            EntityMention(
                chunk_id=str(r["chunk_id"]),
                start_offset=r["start_offset"],
                end_offset=r["end_offset"],
                chunk_preview=r["chunk_preview"],
            )
            for r in mention_rows
        ],
        related=[
            RelatedEntity(
                id=str(r["id"]),
                name=r["name"],
                type=r["type"],
                co_mention_count=int(r["co_mention_count"]),
            )
            for r in related_rows
        ],
    )


# ---------------------------------------------------------------------------
# /relationships  —  paginated list
# ---------------------------------------------------------------------------


@router.get("/relationships", response_model=RelationshipList)
async def list_relationships(
    entity_id: str | None = Query(default=None, description="Either source or target."),
    type: str | None = Query(default=None, description="Filter by relationship type."),
    space: str | None = Query(default=None, description="Filter by chunk's space."),
    limit: int = Query(default=50, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
) -> RelationshipList:
    where: list[str] = []
    params: list = []

    if entity_id is not None:
        where.append("(r.source_entity_id = %s OR r.target_entity_id = %s)")
        params.extend([entity_id, entity_id])
    if type is not None:
        where.append("r.type = %s")
        params.append(type)
    if space is not None:
        where.append(
            "r.chunk_id IN (SELECT c.id FROM chunks c "
            "JOIN memory_spaces ms ON ms.id = c.space_id WHERE ms.name = %s)"
        )
        params.append(space)

    where_sql = " AND ".join(where) if where else "TRUE"

    count_row = await fetch_one(
        f"SELECT COUNT(*) AS total FROM relationships r WHERE {where_sql}",
        tuple(params),
    )
    total = int(count_row["total"]) if count_row else 0

    rows = await fetch_all(
        f"""SELECT r.id, r.source_entity_id, r.target_entity_id, r.type,
                   r.chunk_id, r.created_at,
                   s.name AS source_name, t.name AS target_name
            FROM relationships r
            JOIN entities s ON s.id = r.source_entity_id
            JOIN entities t ON t.id = r.target_entity_id
            WHERE {where_sql}
            ORDER BY r.created_at DESC
            LIMIT %s OFFSET %s""",
        tuple(params + [limit, offset]),
    )

    relationships = [
        RelationshipRow(
            id=str(r["id"]),
            source_entity_id=str(r["source_entity_id"]),
            target_entity_id=str(r["target_entity_id"]),
            source_name=r["source_name"],
            target_name=r["target_name"],
            type=r["type"],
            chunk_id=str(r["chunk_id"]) if r["chunk_id"] else None,
            created_at=r["created_at"],
        )
        for r in rows
    ]

    return RelationshipList(
        relationships=relationships, total=total, limit=limit, offset=offset
    )


# ---------------------------------------------------------------------------
# /visualize  —  force-directed graph nodes and edges
# ---------------------------------------------------------------------------


@router.get("/visualize", response_model=GraphVisualization)
async def visualize(
    space: str | None = Query(default=None),
    type: str | None = Query(default=None),
    min_mentions: int = Query(default=1, ge=1),
    max_nodes: int = Query(default=100, ge=1, le=500),
) -> GraphVisualization:
    where: list[str] = []
    params: list = []

    if space is not None:
        where.append("ms.name = %s")
        params.append(space)
    if type is not None:
        where.append("e.type = %s")
        params.append(type)

    where_sql = " AND ".join(where) if where else "TRUE"

    # Nodes: pick top `max_nodes` by mention_count, filtered by min_mentions.
    node_rows = await fetch_all(
        f"""SELECT e.id, e.name, e.type, COUNT(em.id) AS mention_count
            FROM entities e
            JOIN memory_spaces ms ON ms.id = e.space_id
            LEFT JOIN entity_mentions em ON em.entity_id = e.id
            WHERE {where_sql}
            GROUP BY e.id
            HAVING COUNT(em.id) >= %s
            ORDER BY mention_count DESC, e.name ASC
            LIMIT %s""",
        tuple(params + [min_mentions, max_nodes]),
    )

    # Count what would have been returned without the cap, so the frontend
    # can indicate truncation.
    total_row = await fetch_one(
        f"""SELECT COUNT(*) AS total FROM (
                SELECT e.id
                FROM entities e
                JOIN memory_spaces ms ON ms.id = e.space_id
                LEFT JOIN entity_mentions em ON em.entity_id = e.id
                WHERE {where_sql}
                GROUP BY e.id
                HAVING COUNT(em.id) >= %s
            ) sub""",
        tuple(params + [min_mentions]),
    )
    total_nodes_available = int(total_row["total"]) if total_row else 0

    node_ids = [str(r["id"]) for r in node_rows]
    nodes = [
        GraphNode(
            id=str(r["id"]),
            name=r["name"],
            type=r["type"],
            mention_count=int(r["mention_count"]),
        )
        for r in node_rows
    ]

    # Edges: only those connecting two surviving nodes.
    edges: list[GraphEdge] = []
    if node_ids:
        edge_rows = await fetch_all(
            """SELECT source_entity_id, target_entity_id, type,
                      COUNT(*) AS weight
               FROM relationships
               WHERE source_entity_id = ANY(%s::uuid[])
                 AND target_entity_id = ANY(%s::uuid[])
               GROUP BY source_entity_id, target_entity_id, type
               ORDER BY weight DESC""",
            (node_ids, node_ids),
        )
        edges = [
            GraphEdge(
                source=str(r["source_entity_id"]),
                target=str(r["target_entity_id"]),
                type=r["type"],
                weight=int(r["weight"]),
            )
            for r in edge_rows
        ]

    return GraphVisualization(
        nodes=nodes,
        edges=edges,
        node_count=len(nodes),
        edge_count=len(edges),
        truncated=total_nodes_available > len(nodes),
    )
