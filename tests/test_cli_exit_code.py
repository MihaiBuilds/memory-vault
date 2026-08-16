"""
CLI exit-code behaviour for `memory-vault ingest`.

Regression guard for issue #104: `_cmd_ingest` printed `stats.failed` but
never mapped a non-zero failure count to a non-zero process exit code, so
shell scripts, CI jobs, and agents could not distinguish a complete
ingestion from a failed one by checking ``$?``.

The reproduction is synthetic — the pipeline is replaced with a stub that
returns ``IngestionStats(failed=1)``. No real ingestion or database work is
performed.
"""

from __future__ import annotations

from pathlib import Path

import pytest

import memory_vault.cli as cli_module
import memory_vault.models.db as db_module
import memory_vault.services.ingestion as ingestion_module

pytestmark = pytest.mark.asyncio


class _StubPipeline:
    """Stand-in for IngestionPipeline that returns a fixed IngestionStats."""

    def __init__(self, stats: ingestion_module.IngestionStats):
        self._stats = stats

    def __call__(self, *args, **kwargs):
        return self

    def enqueue(self, *args, **kwargs) -> None:
        pass

    async def run_all(self) -> ingestion_module.IngestionStats:
        return self._stats


async def _install_stubs(
    monkeypatch, stats: ingestion_module.IngestionStats, tmp_path: Path
) -> Path:
    """Patch DB helpers and IngestionPipeline so _cmd_ingest never touches Postgres."""

    async def _noop_init_pool(*args, **kwargs) -> None:
        return None

    async def _noop_close_pool() -> None:
        return None

    async def _fetch_default_space(query, params):
        return {"id": 1}

    monkeypatch.setattr(db_module, "init_pool", _noop_init_pool)
    monkeypatch.setattr(db_module, "close_pool", _noop_close_pool)
    monkeypatch.setattr(db_module, "fetch_one", _fetch_default_space)

    stub = _StubPipeline(stats)
    monkeypatch.setattr(ingestion_module, "IngestionPipeline", stub)

    dummy_file = tmp_path / "input.md"
    dummy_file.write_text("stub content — never actually ingested\n")
    return dummy_file


async def test_cli_ingest_exits_nonzero_when_any_chunk_fails(monkeypatch, tmp_path, capsys):
    """A pipeline that reports ``failed > 0`` must make _cmd_ingest sys.exit(1)."""
    stats = ingestion_module.IngestionStats(failed=1, errors=["stub.md: adapter rejected the file"])
    dummy_file = await _install_stubs(monkeypatch, stats, tmp_path)

    with pytest.raises(SystemExit) as exc_info:
        await cli_module._cmd_ingest(str(dummy_file), space="default")

    assert exc_info.value.code == 1
    out = capsys.readouterr().out
    assert "0 chunks created, 1 failed" in out
    assert "stub.md: adapter rejected the file" in out


async def test_cli_ingest_returns_normally_on_success(monkeypatch, tmp_path, capsys):
    """A pipeline with zero failures must not call sys.exit."""
    stats = ingestion_module.IngestionStats(chunks_created=3, completed=1, failed=0)
    dummy_file = await _install_stubs(monkeypatch, stats, tmp_path)

    # Should not raise SystemExit.
    await cli_module._cmd_ingest(str(dummy_file), space="default")

    out = capsys.readouterr().out
    assert "3 chunks created, 0 failed" in out
