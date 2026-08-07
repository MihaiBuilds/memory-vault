"""
Regression tests for the startup guard that refuses to open a pool when
`EMBEDDING_DIMENSIONS` does not match the connected DB's `chunks.embedding`
vector dimension.

Background: MV exposes EMBEDDING_DIMENSIONS as configuration, but the
initial schema hardcodes `vector(384)`. Any other setting silently succeeds
until the first embedding INSERT/SELECT, which then fails opaquely.

The guard queries pg_attribute for the actual `vector(N)` column dimension
and compares it to `settings.embedding_dimensions` at pool-open time,
raising RuntimeError with a clear message on mismatch.

Integration: shares the memory_vault_test database from conftest.py — it's
already migrated with vector(384) by the session-scoped fixture, so we test
mismatch by monkeypatching settings, not by mutating the schema.
"""

from __future__ import annotations

from dataclasses import replace

import pytest

pytestmark = pytest.mark.asyncio


def _settings_with(monkeypatch, **overrides):
    """Swap `db.settings` for a copy with the given field overrides.

    Settings is a frozen dataclass, so per-attribute monkeypatch doesn't work;
    dataclasses.replace() gives us a new frozen instance with the overrides.
    """
    from memory_vault.models import db

    monkeypatch.setattr(db, "settings", replace(db.settings, **overrides))


class TestVerifyEmbeddingDimension:
    async def test_passes_silently_when_config_matches_schema(self):
        """Default flow: EMBEDDING_DIMENSIONS=384 matches vector(384) schema."""
        from memory_vault.models.db import _verify_embedding_dimension

        # No override — actual settings.embedding_dimensions is 384 by
        # default and the test DB has vector(384). Should not raise.
        await _verify_embedding_dimension()

    async def test_raises_when_config_does_not_match_schema(self, monkeypatch):
        """The regression guard: mismatched config must fail fast with a
        clear error naming both the configured and actual dimensions."""
        from memory_vault.models import db

        _settings_with(monkeypatch, embedding_dimensions=768)
        with pytest.raises(RuntimeError, match=r"EMBEDDING_DIMENSIONS=768.*vector\(384\)"):
            await db._verify_embedding_dimension()

    async def test_error_message_names_both_dimensions(self, monkeypatch):
        """Failure message must give operators both numbers so the fix is
        obvious (either bump config or point at a matching DB)."""
        from memory_vault.models import db

        _settings_with(monkeypatch, embedding_dimensions=1024)
        with pytest.raises(RuntimeError) as exc:
            await db._verify_embedding_dimension()
        msg = str(exc.value)
        assert "1024" in msg
        assert "384" in msg
        assert "EMBEDDING_DIMENSIONS" in msg

    async def test_skips_gracefully_when_chunks_table_missing(self, monkeypatch):
        """Fresh install path: before migration 001 has ever run, the chunks
        table does not exist yet. The guard must skip cleanly rather than
        crash — run_migrations() is the natural next step in that flow."""
        from memory_vault.models import db

        async def _no_row(*_args, **_kwargs):
            return None

        monkeypatch.setattr(db, "fetch_one", _no_row)
        _settings_with(monkeypatch, embedding_dimensions=999)
        # Must not raise, even with a mismatched configured dimension.
        await db._verify_embedding_dimension()
