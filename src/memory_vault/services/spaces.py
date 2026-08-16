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
    """Return `name` reduced to characters a space name may contain.

    Callers reach the log only after validate_space_name has accepted the
    name, so in practice this returns it unchanged. It exists so that the
    guarantee holds at the log call itself rather than depending on a check
    several lines earlier that a later edit could reorder.

    Uses an explicit character class rather than `str.isalnum`, which accepts
    letters from any script and so would pass through more than a space name
    may contain.
    """
    return _LOG_UNSAFE_CHARS.sub("", name)[:SPACE_NAME_MAX_LENGTH]


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
