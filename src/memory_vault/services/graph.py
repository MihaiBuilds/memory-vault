"""Knowledge-graph curation — merging entities the extractor kept apart.

Extraction is per-occurrence and literal, so the same real-world thing arrives
under several names: "Alice", "Alice Smith", "A. Smith". Nothing automatic can
safely decide those are one person, but a human looking at the graph can.
"""

from __future__ import annotations

import logging
from typing import Any
from uuid import UUID

from memory_vault.models.db import get_pool

logger = logging.getLogger(__name__)


class EntityNotFound(LookupError):
    """No entity with the given id."""


class CrossSpaceMerge(ValueError):
    """The two entities live in different memory spaces."""


class SameEntityMerge(ValueError):
    """Both ids refer to the same entity."""


async def merge_entities(winner_id: UUID | str, loser_id: UUID | str) -> dict[str, Any]:
    """Fold `loser_id` into `winner_id`, keeping the graph consistent.

    Everything happens in one transaction, and the order inside it matters —
    each step below exists because doing it the obvious way breaks something
    that was measured, not guessed.

    **The loser is deleted inside the same transaction.** There is a UNIQUE
    index on ``(lower(name), type, space_id)``, so two entities that would
    collide cannot both exist even momentarily. Renaming first and deleting
    after raises ``UniqueViolation``.

    **Self-relationships are dropped, not rewritten.** If the two entities are
    related to each other, rewriting both endpoints produces a row whose source
    and target are the same entity — a node with an edge to itself, which is
    not a fact about anything. Verified: a naive rewrite creates exactly that.

    **Mentions that would land on the same spot are dropped.** A mention is
    (entity, chunk, offsets). If both entities were matched at the same
    offsets in the same chunk, rewriting produces two identical rows. There is
    no unique index to stop it, so nothing complains — the mention count simply
    inflates, and mention counts drive node size and the `min_mentions` filter.
    Distinct offsets are kept: those are genuinely separate occurrences.

    Refuses to merge across spaces. Entities are per-space by design, and a
    cross-space merge would move data between spaces without saying so.
    """
    winner_id = str(winner_id)
    loser_id = str(loser_id)

    if winner_id == loser_id:
        raise SameEntityMerge("An entity cannot be merged into itself.")

    pool = await get_pool()
    async with pool.connection() as conn:
        async with conn.transaction():
            # Lock both rows before reading anything about them, so a
            # concurrent merge of the same pair cannot interleave.
            cur = await conn.execute(
                """SELECT id, name, type, space_id
                   FROM entities WHERE id IN (%s, %s)
                   ORDER BY id
                   FOR UPDATE""",
                (winner_id, loser_id),
            )
            rows = await cur.fetchall()
            found = {str(r["id"]): r for r in rows}

            missing = [i for i in (winner_id, loser_id) if i not in found]
            if missing:
                raise EntityNotFound(f"Entity not found: {missing[0]}")

            winner = found[winner_id]
            loser = found[loser_id]

            if winner["space_id"] != loser["space_id"]:
                raise CrossSpaceMerge(
                    "Entities live in different spaces and cannot be merged. "
                    "Move the memories into one space first."
                )

            # Drop the mentions that would become duplicates of one the winner
            # already has. Same chunk, same offsets, same entity is one fact
            # recorded twice.
            cur = await conn.execute(
                """DELETE FROM entity_mentions loser_m
                   WHERE loser_m.entity_id = %s
                     AND EXISTS (
                         SELECT 1 FROM entity_mentions winner_m
                         WHERE winner_m.entity_id = %s
                           AND winner_m.chunk_id = loser_m.chunk_id
                           AND winner_m.start_offset = loser_m.start_offset
                           AND winner_m.end_offset = loser_m.end_offset
                     )""",
                (loser_id, winner_id),
            )
            duplicate_mentions_dropped = cur.rowcount

            cur = await conn.execute(
                "UPDATE entity_mentions SET entity_id = %s WHERE entity_id = %s",
                (winner_id, loser_id),
            )
            mentions_moved = cur.rowcount

            # Relationships between the two entities would become self-loops.
            cur = await conn.execute(
                """DELETE FROM relationships
                   WHERE (source_entity_id = %s AND target_entity_id = %s)
                      OR (source_entity_id = %s AND target_entity_id = %s)""",
                (winner_id, loser_id, loser_id, winner_id),
            )
            self_relationships_dropped = cur.rowcount

            cur = await conn.execute(
                "UPDATE relationships SET source_entity_id = %s WHERE source_entity_id = %s",
                (winner_id, loser_id),
            )
            sources_moved = cur.rowcount

            cur = await conn.execute(
                "UPDATE relationships SET target_entity_id = %s WHERE target_entity_id = %s",
                (winner_id, loser_id),
            )
            targets_moved = cur.rowcount

            # Last, and inside the transaction: the UNIQUE index means the
            # loser cannot outlive this statement if the two share a name.
            await conn.execute("DELETE FROM entities WHERE id = %s", (loser_id,))

    logger.info(
        "Merged entity %s into %s: %d mentions, %d relationship endpoints",
        loser["name"],
        winner["name"],
        mentions_moved,
        sources_moved + targets_moved,
    )

    return {
        "winner_id": winner_id,
        "winner_name": winner["name"],
        "merged_name": loser["name"],
        "mentions_moved": mentions_moved,
        "relationships_moved": sources_moved + targets_moved,
        "duplicate_mentions_dropped": duplicate_mentions_dropped,
        "self_relationships_dropped": self_relationships_dropped,
    }
