"""
Persist extracted entities and relationships to the knowledge graph tables.

All writes for a single chunk happen in one transaction separate from the
chunk insert itself. If any write in the graph transaction fails, the
whole graph transaction is rolled back but the chunk remains — the
chunk is always more important than its graph annotations.
"""

from __future__ import annotations

import logging

from src.extraction.spacy_extractor import Entity, Relationship
from src.models.db import get_pool

logger = logging.getLogger(__name__)


async def write_graph_for_chunk(
    chunk_id: str,
    space_id: int,
    entities: list[Entity],
    relationships: list[Relationship],
) -> None:
    """Write entities, mentions, and relationships for one chunk atomically.

    Logs and swallows exceptions — the chunk is already persisted; a
    failed graph write must not break ingestion.
    """
    if not entities:
        return

    try:
        pool = await get_pool()
        async with pool.connection() as conn:
            try:
                # 1. Upsert every entity, collecting id by (lower(name), type).
                entity_ids: dict[tuple[str, str], str] = {}
                for ent in entities:
                    cur = await conn.execute(
                        """INSERT INTO entities (name, type, space_id)
                           VALUES (%s, %s, %s)
                           ON CONFLICT (lower(name), type, space_id)
                           DO UPDATE SET name = EXCLUDED.name
                           RETURNING id""",
                        (ent.name, ent.type, space_id),
                    )
                    row = await cur.fetchone()
                    entity_ids[(ent.name.lower(), ent.type)] = row["id"]

                # 2. One entity_mentions row per extracted entity occurrence.
                for ent in entities:
                    entity_id = entity_ids[(ent.name.lower(), ent.type)]
                    await conn.execute(
                        """INSERT INTO entity_mentions
                               (entity_id, chunk_id, start_offset, end_offset)
                           VALUES (%s, %s, %s, %s)""",
                        (entity_id, chunk_id, ent.start, ent.end),
                    )

                # 3. Relationships (M7: only `related_to` from co-occurrence).
                #    Extractor returns pairs keyed by entity name; resolve to ids.
                #    Entity type is needed to disambiguate — the extractor pairs
                #    entities within a single chunk, so both sides always exist
                #    in entity_ids. We look up by name alone because co-occurrence
                #    pairs don't carry type info; collisions within a chunk on
                #    (name, different type) are rare and fall back to skipping.
                name_to_ids: dict[str, list[str]] = {}
                for ent in entities:
                    name_to_ids.setdefault(ent.name, []).append(
                        entity_ids[(ent.name.lower(), ent.type)]
                    )

                for rel in relationships:
                    source_ids = name_to_ids.get(rel.source_name, [])
                    target_ids = name_to_ids.get(rel.target_name, [])
                    if not source_ids or not target_ids:
                        continue
                    # Take the first match on each side. Same-name-different-type
                    # disambiguation is future work — rare enough to ignore for M7.
                    await conn.execute(
                        """INSERT INTO relationships
                               (source_entity_id, target_entity_id, type, chunk_id)
                           VALUES (%s, %s, %s, %s)""",
                        (source_ids[0], target_ids[0], rel.type, chunk_id),
                    )

                await conn.commit()
            except Exception:
                await conn.rollback()
                raise
    except Exception:
        logger.exception(
            "Graph write failed for chunk %s (space %s) — chunk retained, graph data absent",
            chunk_id,
            space_id,
        )
