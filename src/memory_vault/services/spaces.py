"""Memory space resolution and on-demand creation.

Shared by the routers that need a space id, so the rules about which names
are legal live in one place rather than being restated at each call site.
"""

from __future__ import annotations

import logging
import re

from memory_vault.models.db import execute_returning, fetch_one

logger = logging.getLogger(__name__)

# Mirrors the constraint on SpaceCreateRequest.name. Kept as a compiled pattern
# here because auto-creation happens below the request-schema layer, where
# pydantic has already validated the explicit-create path but not this one.
SPACE_NAME_PATTERN = re.compile(r"^[a-z0-9][a-z0-9-]*$")
SPACE_NAME_MAX_LENGTH = 64

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


async def get_space_id(name: str) -> int | None:
    """Return the id of an existing space, or None."""
    row = await fetch_one("SELECT id FROM memory_spaces WHERE name = %s", (name,))
    return int(row["id"]) if row else None


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
        logger.info("Created space on first write: %s", name)
        return int(row["id"])

    # The conflict fired: another caller created it between our read and our
    # insert. Its row is committed, so this read finds it.
    created_by_other = await get_space_id(name)
    if created_by_other is None:
        raise InvalidSpaceName(f"Space could not be created: {name}")
    return created_by_other
