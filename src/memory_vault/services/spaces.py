"""Memory space resolution and on-demand creation.

Shared by the routers that need a space id, so the rules about which names
are legal live in one place rather than being restated at each call site.
"""

from __future__ import annotations

import logging
import re
from typing import Any

from memory_vault.models.db import execute_returning, fetch_one, get_pool

logger = logging.getLogger(__name__)

# Mirrors the constraint on SpaceCreateRequest.name. Kept as a compiled pattern
# here because auto-creation happens below the request-schema layer, where
# pydantic has already validated the explicit-create path but not this one.
SPACE_NAME_PATTERN = re.compile(r"^[a-z0-9][a-z0-9-]*$")
SPACE_NAME_MAX_LENGTH = 64

# Everything a space name may not contain. Applied before a name is written to
# a log, so a value that somehow bypassed validation still cannot introduce a
# line break and forge an entry.
_LOG_UNSAFE_CHARS = re.compile(r"[^a-z0-9-]")

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


class InvalidSpaceName(ValueError):
    """A space name that may not be created."""


def validate_space_name(name: str) -> None:
    """Raise InvalidSpaceName unless `name` may be created.

    Applies the same rules as the explicit create endpoint, so a name the API
    would refuse cannot slip in through a path that creates spaces on demand.
    """
    if not name:
        raise InvalidSpaceName("Space name must not be empty.")
    if len(name) > SPACE_NAME_MAX_LENGTH:
        raise InvalidSpaceName(
            f"Space name exceeds {SPACE_NAME_MAX_LENGTH} characters: {name[:80]}"
        )
    if name in RESERVED_SPACE_NAMES:
        raise InvalidSpaceName(f"Space name is reserved: {name}")
    if not SPACE_NAME_PATTERN.match(name):
        raise InvalidSpaceName(
            "Space name must use lowercase letters, digits, and hyphens, "
            f"and start with a letter or digit: {name}"
        )


def _sanitized_for_log(name: str) -> str:
    """Return `name` in a form that is safe to emit in a log line.

    Callers reach the log only after validate_space_name has accepted the
    name, so in practice this returns it unchanged. It exists so that the
    guarantee holds at the log call itself rather than depending on a check
    several lines earlier that a later edit could reorder.

    Line breaks are removed explicitly before the character class is applied.
    The class would drop them anyway, so the output is the same either way,
    but log forging is what this function exists to prevent and it is worth
    stating in the code rather than leaving as a consequence of the pattern.
    An explicit class is used rather than `str.isalnum`, which is true for
    letters in any script and would pass through more than a name may hold.
    """
    no_line_breaks = name.replace("\r", "").replace("\n", "")
    return _LOG_UNSAFE_CHARS.sub("", no_line_breaks)[:SPACE_NAME_MAX_LENGTH]


async def get_space_id(name: str) -> int | None:
    """Return the id of an existing space, or None."""
    row = await fetch_one("SELECT id FROM memory_spaces WHERE name = %s", (name,))
    return int(row["id"]) if row else None


class ChunkNotFound(LookupError):
    """No chunk with the given id."""


class SpaceNotFound(LookupError):
    """No space with the given name."""


class SpaceNotEmpty(ValueError):
    """A space that still holds memories or graph entities."""


class SpaceReserved(ValueError):
    """A space that may not be deleted regardless of its contents."""


async def delete_space(name: str) -> None:
    """Delete an empty, non-reserved space.

    Refuses rather than cascades. The two foreign keys pointing at a space
    behave differently, and neither is a safe thing to trigger by accident:

    - `chunks.space_id` is ON DELETE NO ACTION, so Postgres refuses the delete
      outright. Without a check first that surfaces as a 500 on a raw
      IntegrityError rather than as an answer.
    - `entities.space_id` is ON DELETE CASCADE, and entities cascade further
      into `entity_mentions` and `relationships`. That one does not fail — it
      silently destroys the space's whole subgraph.

    So emptiness is checked against both tables. A space holding only entities
    has no memories in it but is very much not empty, and deleting it would
    take the graph with it.

    Chunks are counted unfiltered, unlike the `chunk_count` in the space list,
    which counts only live ones. A space whose memories have all been forgotten
    displays as empty while its rows are still present, and those rows still
    trip the foreign key. Counting what the UI shows would let this pass its
    own check and then fail in the database.
    """
    if name in RESERVED_SPACE_NAMES:
        raise SpaceReserved(f"Space name is reserved: {name}")

    space_id = await get_space_id(name)
    if space_id is None:
        raise SpaceNotFound(f"Unknown space: {name}")

    # Counting and deleting in one transaction, so an ingest landing between
    # the two cannot turn a passed check into a foreign-key error. The row is
    # locked first: without it the count is a snapshot that another session is
    # free to invalidate before the DELETE runs.
    pool = await get_pool()
    async with pool.connection() as conn:
        async with conn.transaction():
            await conn.execute("SELECT id FROM memory_spaces WHERE id = %s FOR UPDATE", (space_id,))

            cur = await conn.execute(
                """SELECT (SELECT COUNT(*) FROM chunks WHERE space_id = %s) AS chunks,
                          (SELECT COUNT(*) FROM entities WHERE space_id = %s) AS entities""",
                (space_id, space_id),
            )
            counts = await cur.fetchone()
            chunk_count = int(counts["chunks"])
            entity_count = int(counts["entities"])

            if chunk_count or entity_count:
                raise SpaceNotEmpty(
                    f"Space is not empty: {name} "
                    f"({chunk_count} memories, {entity_count} graph entities). "
                    "Move or delete its contents first."
                )

            await conn.execute("DELETE FROM memory_spaces WHERE id = %s", (space_id,))

    logger.info("Space deleted: %s", _sanitized_for_log(name))


async def move_chunk(chunk_id: str, target_space: str) -> dict[str, Any]:
    """Move a chunk into `target_space`, keeping its graph entries consistent.

    The target space must already exist: moving is a curation step on stored
    memories, not a way to bring a space into being, and a typo here should
    fail rather than scatter memories into a new near-identical space.

    The chunk's embedding is unchanged — only its space changes, so the text
    is never re-embedded. Its extracted entities are another matter. Entities
    are per-space, while mentions hang off the chunk, so a chunk that moves
    without them leaves its entities behind in the space it came from and the
    graph disagrees with search about where the memory lives. The mentions and
    relationships are therefore dropped and extraction is re-run against the
    target space. Entities left with no live mentions fall out of the graph
    views on their own.
    """
    from memory_vault.services.ingestion import _run_extraction

    chunk = await fetch_one(
        """SELECT c.id, c.content, c.space_id, ms.name AS space_name
           FROM chunks c JOIN memory_spaces ms ON ms.id = c.space_id
           WHERE c.id = %s""",
        (chunk_id,),
    )
    if chunk is None:
        raise ChunkNotFound(f"Chunk not found: {chunk_id}")

    target_id = await get_space_id(target_space)
    if target_id is None:
        raise SpaceNotFound(f"Unknown space: {target_space}")

    source_space = str(chunk["space_name"])
    if int(chunk["space_id"]) == target_id:
        return {
            "chunk_id": str(chunk["id"]),
            "from_space": source_space,
            "to_space": target_space,
            "moved": False,
        }

    pool = await get_pool()
    async with pool.connection() as conn:
        async with conn.transaction():
            await conn.execute(
                "UPDATE chunks SET space_id = %s, updated_at = now() WHERE id = %s",
                (target_id, chunk_id),
            )
            await conn.execute("DELETE FROM entity_mentions WHERE chunk_id = %s", (chunk_id,))
            await conn.execute("DELETE FROM relationships WHERE chunk_id = %s", (chunk_id,))

    # Outside the transaction: extraction is best-effort and must not undo a
    # move that has already succeeded.
    await _run_extraction(str(chunk["id"]), str(chunk["content"]), target_id)

    return {
        "chunk_id": str(chunk["id"]),
        "from_space": source_space,
        "to_space": target_space,
        "moved": True,
    }


async def ensure_space(name: str) -> int:
    """Return the id of `name`, creating the space if it does not exist.

    Raises InvalidSpaceName when the name may not be created. Two callers
    racing on the same new name is expected — the unique constraint on
    memory_spaces.name settles it and the loser reads back the winner's row,
    so both get the same id rather than one seeing an integrity error.
    """
    existing = await get_space_id(name)
    if existing is not None:
        return existing

    validate_space_name(name)

    row = await execute_returning(
        """INSERT INTO memory_spaces (name) VALUES (%s)
           ON CONFLICT (name) DO NOTHING
           RETURNING id""",
        (name,),
    )
    if row is not None:
        # Log a value re-derived from the pattern rather than the caller's
        # string. validate_space_name has already rejected anything carrying a
        # newline or control character, so this cannot change what is written;
        # it makes the sanitisation visible at the point of use, to readers and
        # to static analysis alike.
        logger.info("Created space on first write: %s", _sanitized_for_log(name))
        return int(row["id"])

    # The conflict fired: another caller created it between our read and our
    # insert. Its row is committed, so this read finds it.
    created_by_other = await get_space_id(name)
    if created_by_other is None:
        raise InvalidSpaceName(f"Space could not be created: {name}")
    return created_by_other
