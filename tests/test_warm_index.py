"""
Vector index warm-up.

Without this the first real search after a start pays to read the HNSW index
off disk — on a large corpus that is the slowest query anyone will run, and it
lands on a user rather than on start-up.

The property that matters more than the speed-up: warming must never stop the
process from starting. It is an optimisation, and a container that refuses to
boot because an optimisation failed is worse than a slow first query.
"""

from __future__ import annotations

import pytest

from memory_vault.cli import WARM_INDEX_SQL, _cmd_warm_index
from memory_vault.models.db import fetch_all


@pytest.fixture
def keep_pool_open(monkeypatch):
    """
    `_cmd_warm_index` closes the pool in its `finally`. That is right for a
    one-shot CLI command and wrong to let happen here: conftest opens a single
    pool for the whole session, so a real close ends the suite — every
    subsequent test errors with `PoolClosed` in the truncate fixture.

    So the close is intercepted and recorded rather than performed. The
    recording is what the closing test asserts on; the interception is what
    keeps the rest of the suite alive.
    """
    from memory_vault.models import db as db_mod

    calls: list[str] = []

    async def _record_close():
        calls.append("close_pool")

    monkeypatch.setattr(db_mod, "close_pool", _record_close)
    return calls


class TestWarmingTouchesTheIndex:
    async def test_the_index_exists_to_be_warmed(self):
        """If the index is ever renamed, the warm query stops being useful."""
        rows = await fetch_all(
            "SELECT indexname FROM pg_indexes WHERE indexname = 'chunks_embedding_idx'"
        )
        assert rows, "chunks_embedding_idx is what the warm query is meant to page in"

    async def test_the_warm_query_actually_uses_the_index(self):
        """
        A query that reads the table but not the index would warm nothing —
        and would look identical from the outside, since it succeeds, prints a
        timing and returns no rows on an empty corpus. The plan is the only
        thing that distinguishes them.

        EXPLAINs `WARM_INDEX_SQL` itself rather than a copy: an earlier version
        of this test pasted the query in, and a mutation that made the real one
        stop using the index left it passing.
        """
        from memory_vault.config import settings

        zero = "[" + ",".join(["0"] * settings.embedding_dimensions) + "]"
        rows = await fetch_all(f"EXPLAIN {WARM_INDEX_SQL}", (zero,))
        plan = " ".join(r["QUERY PLAN"] for r in rows)
        assert "chunks_embedding_idx" in plan, f"warm query did not use the index:\n{plan}"

    async def test_warming_works_on_an_empty_corpus(self, keep_pool_open, capsys):
        """
        A fresh install has no chunks. Warming must be a no-op there, not an
        error — this runs on every start, including the first.
        """
        await _cmd_warm_index()

        out = capsys.readouterr().out
        assert "Vector index warmed in" in out, f"expected a successful warm, got: {out!r}"
        assert "skipped" not in out


class TestWarmingNeverBlocksStartup:
    """
    `scripts/start.sh` runs under `set -e`. If this command raised or exited
    non-zero, a failed warm-up would stop the container from starting.
    """

    async def test_database_unavailable_is_survived(self, monkeypatch, keep_pool_open, capsys):
        """The container starting before Postgres accepts connections."""
        from memory_vault.models import db as db_mod

        async def _refuse(*args, **kwargs):
            raise OSError("connection refused")

        monkeypatch.setattr(db_mod, "init_pool", _refuse)

        await _cmd_warm_index()  # must return, not raise

        out = capsys.readouterr().out
        assert "database unavailable" in out
        assert "connection refused" in out, "the operator should see why it was skipped"

    async def test_query_failure_is_survived(self, monkeypatch, keep_pool_open, capsys):
        """
        The pool opens but the query fails — a missing table during a partial
        migration, a permissions problem, a statement timeout.
        """
        from memory_vault.models import db as db_mod

        async def _explode(*args, **kwargs):
            raise RuntimeError('relation "chunks" does not exist')

        monkeypatch.setattr(db_mod, "fetch_all", _explode)

        await _cmd_warm_index()  # must return, not raise

        out = capsys.readouterr().out
        assert "Index warm-up skipped" in out
        assert "does not exist" in out

    async def test_pool_is_closed_even_when_the_query_fails(self, monkeypatch, keep_pool_open):
        """
        Leaking a pool on every failed start would exhaust connections on a
        crash-looping container.
        """
        from memory_vault.models import db as db_mod

        async def _explode(*args, **kwargs):
            raise RuntimeError("boom")

        monkeypatch.setattr(db_mod, "fetch_all", _explode)

        await _cmd_warm_index()

        assert keep_pool_open == ["close_pool"], (
            "the pool must be closed exactly once, even on the failure path"
        )


class TestStartupScript:
    """The command is only useful if start.sh actually calls it."""

    def test_start_sh_warms_after_migrating(self):
        from pathlib import Path

        script = (Path(__file__).resolve().parent.parent / "scripts" / "start.sh").read_text()

        assert "memory-vault warm-index" in script, "start.sh should warm the index"

        migrate_at = script.index("memory-vault migrate")
        warm_at = script.index("memory-vault warm-index")
        assert migrate_at < warm_at, "warming must come after migrations create the index"

    def test_start_sh_does_not_let_warming_fail_the_boot(self):
        from pathlib import Path

        script = (Path(__file__).resolve().parent.parent / "scripts" / "start.sh").read_text()

        warm_line = next(line for line in script.splitlines() if "memory-vault warm-index" in line)
        assert "|| true" in warm_line, (
            "start.sh runs under set -e; the warm call needs an explicit guard"
        )
