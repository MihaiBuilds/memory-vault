"""Knowledge graph endpoints — entities, relationships, and visualization."""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status

from memory_vault.api.deps import require_token
from memory_vault.api.schemas import (
    EntityDetail,
    EntityList,
    EntityMention,
    EntityMergeRequest,
    EntityMergeResponse,
    EntitySummary,
    GraphEdge,
    GraphNode,
    GraphVisualization,
    RelatedEntity,
    RelationshipList,
    RelationshipRow,
)
from memory_vault.models.db import fetch_all, fetch_one
from memory_vault.services.graph import (
    CrossSpaceMerge,
    EntityNotFound,
    SameEntityMerge,
    merge_entities,
)

router = APIRouter(prefix="/api/graph", tags=["graph"], dependencies=[Depends(require_token)])

CHUNK_PREVIEW_LEN = 200

# An upper bound on how many types one request may filter by. There are four
# entity types today, but relationship types are free-form strings written by
# the extractor, so this is not a small closed set. The cap keeps a hostile
# caller from sending a megabyte of comma-separated values.
MAX_TYPE_FILTERS = 50


def parse_type_filter(raw: str | None) -> list[str] | None:
    """Split a comma-separated `type` parameter into the types to filter by.

    `?type=Person` and `?type=Person,Tool` are both valid; the single-value
    form is just a list of one, so every existing caller keeps working and
    there is one code path rather than two.

    Returns None when there is nothing to filter by — no parameter at all, or
    a value that is empty once separators and whitespace are removed. That is
    deliberately the same as omitting it: `?type=` and `?type=,,` mean "no type
    filter", not "match the empty type", which no entity has.

    Duplicates are collapsed and order is preserved, so `?type=Tool,Tool`
    binds one value rather than making the array grow with repetition.
    """
    if raw is None:
        return None

    seen: dict[str, None] = {}
    for part in raw.split(","):
        cleaned = part.strip()
        if cleaned:
            seen[cleaned] = None

    if not seen:
        return None

    return list(seen)[:MAX_TYPE_FILTERS]


# ---------------------------------------------------------------------------
# /entities  —  paginated list
# ---------------------------------------------------------------------------


@router.get("/entities", response_model=EntityList)
async def list_entities(
    space: str | None = Query(default=None, description="Filter by space name."),
    type: str | None = Query(
        default=None,
        description="Filter by entity type. Comma-separated for several: Person,Tool",
    ),
    min_mentions: int = Query(default=1, ge=1),
    limit: int = Query(default=50, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
) -> EntityList:
    where: list[str] = []
    params: list = []

    if space is not None:
        where.append("ms.name = %s")
        params.append(space)
    types = parse_type_filter(type)
    if types is not None:
        where.append("e.type = ANY(%s)")
        params.append(types)

    where_sql = " AND ".join(where) if where else "TRUE"

    # nosec B608 — `where_sql` is composed from a closed list of literal
    # templates ("ms.name = %s", "e.type = ANY(%s)"). User values go through %s
    # parameters in `params`; the type filter binds a list, so the number of
    # values never changes the SQL text. No user-controlled SQL fragments.
    # Subquery builds entity + mention_count, then filters by min_mentions.
    # Reading mentions through live_entity_mentions keeps forgotten chunks out
    # of the count, so entities with zero live mentions drop out via HAVING.
    base_sql = f"""
        SELECT e.id, e.name, e.type, ms.name AS space_name, e.created_at,
               COUNT(em.id) AS mention_count
        FROM entities e
        JOIN memory_spaces ms ON ms.id = e.space_id
        LEFT JOIN live_entity_mentions em ON em.entity_id = e.id
        WHERE {where_sql}
        GROUP BY e.id, ms.name
        HAVING COUNT(em.id) >= %s
    """  # nosec B608

    count_sql = f"SELECT COUNT(*) AS total FROM ({base_sql}) sub"  # nosec B608
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
# /entities/merge  —  fold one entity into another
#
# Declared before /entities/{entity_id} so the intent is obvious at a glance.
# They do not actually collide — this is a POST and that a GET, and the path
# parameter is typed UUID — but a reader should not have to work that out.
# ---------------------------------------------------------------------------


@router.post("/entities/merge", response_model=EntityMergeResponse)
async def merge_entities_endpoint(req: EntityMergeRequest) -> EntityMergeResponse:
    """Fold one entity into another.

    Extraction is literal and per-occurrence, so one real thing often ends up
    as several entities — "Alice", "Alice Smith", "A. Smith". Deciding those
    are the same is a judgement call, so it is offered rather than guessed.
    """
    try:
        result = await merge_entities(req.winner_id, req.loser_id)
    except SameEntityMerge as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e)) from e
    except EntityNotFound as e:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(e)) from e
    except CrossSpaceMerge as e:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(e)) from e

    dropped = []
    if result["duplicate_mentions_dropped"]:
        dropped.append(f"{result['duplicate_mentions_dropped']} duplicate mentions")
    if result["self_relationships_dropped"]:
        dropped.append(f"{result['self_relationships_dropped']} self-relationships")
    tail = f" ({', '.join(dropped)} dropped)" if dropped else ""

    return EntityMergeResponse(
        **result,
        message=(
            f"Merged '{result['merged_name']}' into '{result['winner_name']}': "
            f"{result['mentions_moved']} mentions, "
            f"{result['relationships_moved']} relationship endpoints{tail}"
        ),
    )


# ---------------------------------------------------------------------------
# /entities/{id}  —  detail + mentions + related
# ---------------------------------------------------------------------------


@router.get("/entities/{entity_id}", response_model=EntityDetail)
async def get_entity(entity_id: UUID) -> EntityDetail:
    # mention_count reflects only mentions on live (non-forgotten) chunks.
    entity = await fetch_one(
        """SELECT e.id, e.name, e.type, ms.name AS space_name, e.created_at,
                  (SELECT COUNT(*)
                     FROM live_entity_mentions em
                    WHERE em.entity_id = e.id) AS mention_count
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

    # All live mentions with a short chunk preview, newest first. The view
    # keeps forgotten chunks out, so their preview text never surfaces here.
    mention_rows = await fetch_all(
        """SELECT em.chunk_id, em.start_offset, em.end_offset,
                  LEFT(c.content, %s) AS chunk_preview
           FROM live_entity_mentions em
           JOIN chunks c ON c.id = em.chunk_id
           WHERE em.entity_id = %s
           ORDER BY em.created_at DESC""",
        (CHUNK_PREVIEW_LEN, entity_id),
    )

    # Related entities via relationships (both directions), aggregated. The
    # view drops relationships backed by a forgotten chunk while keeping those
    # with no backing chunk at all.
    related_rows = await fetch_all(
        """SELECT other.id, other.name, other.type, COUNT(*) AS co_mention_count
           FROM live_relationships r
           JOIN entities other ON other.id = CASE
               WHEN r.source_entity_id = %s THEN r.target_entity_id
               ELSE r.source_entity_id
           END
           WHERE (r.source_entity_id = %s OR r.target_entity_id = %s)
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
    type: str | None = Query(
        default=None,
        description=(
            "Filter by relationship type. Comma-separated for several: works_on,related_to"
        ),
    ),
    space: str | None = Query(default=None, description="Filter by chunk's space."),
    limit: int = Query(default=50, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
) -> RelationshipList:
    # Reading from live_relationships excludes rows backed by a forgotten
    # chunk, while keeping those with chunk_id IS NULL (future manual or
    # LLM-tagged links, which have no backing chunk to forget).
    where: list[str] = []
    params: list = []

    if entity_id is not None:
        where.append("(r.source_entity_id = %s OR r.target_entity_id = %s)")
        params.extend([entity_id, entity_id])
    types = parse_type_filter(type)
    if types is not None:
        where.append("r.type = ANY(%s)")
        params.append(types)
    if space is not None:
        where.append(
            "r.chunk_id IN (SELECT c.id FROM chunks c "
            "JOIN memory_spaces ms ON ms.id = c.space_id WHERE ms.name = %s)"
        )
        params.append(space)

    where_sql = " AND ".join(where) if where else "TRUE"

    # nosec B608 — `where_sql` is composed from a closed set of literal
    # templates; user values are bound via %s parameters.
    count_row = await fetch_one(
        f"SELECT COUNT(*) AS total FROM live_relationships r WHERE {where_sql}",  # nosec B608
        tuple(params),
    )
    total = int(count_row["total"]) if count_row else 0

    rows = await fetch_all(
        f"""SELECT r.id, r.source_entity_id, r.target_entity_id, r.type,
                   r.chunk_id, r.created_at,
                   s.name AS source_name, t.name AS target_name
            FROM live_relationships r
            JOIN entities s ON s.id = r.source_entity_id
            JOIN entities t ON t.id = r.target_entity_id
            WHERE {where_sql}
            ORDER BY r.created_at DESC
            LIMIT %s OFFSET %s""",  # nosec B608
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

    return RelationshipList(relationships=relationships, total=total, limit=limit, offset=offset)


# ---------------------------------------------------------------------------
# /visualize  —  force-directed graph nodes and edges
# ---------------------------------------------------------------------------


@router.get("/visualize", response_model=GraphVisualization)
async def visualize(
    space: str | None = Query(default=None),
    type: str | None = Query(
        default=None,
        description="Filter by entity type. Comma-separated for several: Person,Tool",
    ),
    min_mentions: int = Query(default=1, ge=1),
    max_nodes: int = Query(default=100, ge=1, le=500),
) -> GraphVisualization:
    where: list[str] = []
    params: list = []

    if space is not None:
        where.append("ms.name = %s")
        params.append(space)
    types = parse_type_filter(type)
    if types is not None:
        where.append("e.type = ANY(%s)")
        params.append(types)

    where_sql = " AND ".join(where) if where else "TRUE"

    # nosec B608 — `where_sql` composed from closed-set literal templates;
    # user values bound via %s parameters.
    # Nodes: pick top `max_nodes` by mention_count, filtered by min_mentions.
    # Reading mentions through the view keeps forgotten chunks out of the
    # count, so nodes with zero live mentions drop out via HAVING.
    node_rows = await fetch_all(
        f"""SELECT e.id, e.name, e.type, COUNT(em.id) AS mention_count
            FROM entities e
            JOIN memory_spaces ms ON ms.id = e.space_id
            LEFT JOIN live_entity_mentions em ON em.entity_id = e.id
            WHERE {where_sql}
            GROUP BY e.id
            HAVING COUNT(em.id) >= %s
            ORDER BY mention_count DESC, e.name ASC
            LIMIT %s""",  # nosec B608
        tuple(params + [min_mentions, max_nodes]),
    )

    # Count what would have been returned without the cap, so the frontend
    # can indicate truncation. Same live-mention filter as the node query.
    total_row = await fetch_one(
        f"""SELECT COUNT(*) AS total FROM (
                SELECT e.id
                FROM entities e
                JOIN memory_spaces ms ON ms.id = e.space_id
                LEFT JOIN live_entity_mentions em ON em.entity_id = e.id
                WHERE {where_sql}
                GROUP BY e.id
                HAVING COUNT(em.id) >= %s
            ) sub""",  # nosec B608
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

    # Edges: only those connecting two surviving nodes. The view drops edges
    # backed by a forgotten chunk and keeps unbacked ones (chunk_id IS NULL,
    # for future manual or LLM-tagged links).
    edges: list[GraphEdge] = []
    if node_ids:
        edge_rows = await fetch_all(
            """SELECT source_entity_id, target_entity_id, type,
                      COUNT(*) AS weight
               FROM live_relationships r
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
