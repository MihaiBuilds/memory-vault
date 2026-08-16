"""
Database credentials with URI-reserved characters must round-trip cleanly.

Regression guard for issue #113: `Settings.database_url` interpolated
`DB_USER` and `DB_PASSWORD` directly into a `postgresql://...` URI, so
credentials containing `/`, `@`, `:`, `#`, and other URI-reserved
characters were reinterpreted by psycopg's URI parser instead of being
preserved as credential characters. The application could not connect
even though the underlying PostgreSQL credential was valid.

The fix builds the conninfo string via `psycopg.conninfo.make_conninfo`,
which uses keyword=value syntax with proper quoting. These tests parse
each generated conninfo with `psycopg.conninfo.conninfo_to_dict` and
assert every credential character survives unchanged, matching Leonard's
reproduction recipe (no database connection required).
"""

from __future__ import annotations

import os
from dataclasses import replace

import pytest
from psycopg.conninfo import conninfo_to_dict

from memory_vault.config import Settings

# Passwords covering each URI-reserved character class listed in the issue.
RESERVED_PASSWORDS = [
    "left/right",  # forward slash — would look like a database path
    "user@host",  # at-sign — would look like a hostname delimiter
    "colon:sep",  # colon — would look like the user:password separator
    "hash#fragment",  # hash — would look like a URI fragment
    "p@ssw:rd/with#chars",  # combined worst case
    "spaces and tabs\t",  # whitespace — psycopg needs to quote these too
    "single'quote",  # single quote — psycopg conninfo delimiter
    'double"quote',  # double quote — psycopg conninfo delimiter
    "back\\slash",  # backslash
    "unicode:π/λ@θ",  # non-ASCII (real-world names + reserved chars mixed)
    "",  # empty password is legal in some setups
]


@pytest.mark.parametrize("password", RESERVED_PASSWORDS)
def test_database_url_round_trips_password_with_reserved_chars(password: str, monkeypatch):
    """Every credential character must reach psycopg unchanged."""
    # Build a Settings instance with the test password without touching the
    # process-wide `settings` singleton.
    base = Settings()
    tweaked = replace(base, db_password=password)

    parsed = conninfo_to_dict(tweaked.database_url)
    assert parsed.get("password", "") == password, (
        f"password round-trip failed: input={password!r} parsed={parsed.get('password')!r}"
    )
    # Other credential fields must also survive.
    assert parsed.get("user") == base.db_user
    assert parsed.get("host") == base.db_host
    assert parsed.get("dbname") == base.db_name


@pytest.mark.parametrize(
    "user",
    ["user/name", "user@domain", "role:priv", "with space", "unicode-π-role"],
)
def test_database_url_round_trips_user_with_reserved_chars(user: str):
    """DB_USER must also round-trip through the conninfo builder."""
    base = Settings()
    tweaked = replace(base, db_user=user)

    parsed = conninfo_to_dict(tweaked.database_url)
    assert parsed.get("user") == user, (
        f"user round-trip failed: input={user!r} parsed={parsed.get('user')!r}"
    )


def test_database_url_returns_conninfo_string_not_uri():
    """Explicit contract: database_url is now a conninfo string, not a URI."""
    conninfo = Settings().database_url
    # A URI would start with the postgresql:// scheme; the conninfo string
    # is keyword=value pairs and never does.
    assert not conninfo.startswith("postgresql://")
    parsed = conninfo_to_dict(conninfo)
    # All five expected keys are present regardless of default values.
    assert set(parsed.keys()) >= {"host", "port", "dbname", "user", "password"}


def test_env_var_default_password_still_parses():
    """Sanity: the default `memory_vault` password round-trips (baseline)."""
    parsed = conninfo_to_dict(Settings().database_url)
    assert parsed.get("password") == os.environ.get("DB_PASSWORD", "memory_vault")
