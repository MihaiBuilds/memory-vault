"""
Logging must not be able to stop the process from starting.

A read-only root filesystem with nothing mounted at the log path is a
supported deployment — it is what the shipped compose file produces if the
log volume is removed. The log directory is baked into the image, so the
`mkdir` guard succeeds and the failure lands on opening the file instead.
Before this was handled, that combination crashed at boot with a traceback
from inside the logging module.
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path

import pytest

from memory_vault import logging_config


@pytest.fixture(autouse=True)
def _restore_root_logger():
    """configure_logging() mutates the root logger; put it back afterwards."""
    root = logging.getLogger()
    saved = list(root.handlers), root.level
    yield
    root.handlers, root.level = list(saved[0]), saved[1]


def test_unwritable_log_file_does_not_prevent_startup(tmp_path, monkeypatch):
    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    log_file = log_dir / "app.jsonl"

    monkeypatch.setattr(logging_config, "_resolve_log_file", lambda: log_file)

    def _refuse(*args, **kwargs):
        raise OSError(30, "Read-only file system")

    monkeypatch.setattr(logging.handlers, "TimedRotatingFileHandler", _refuse)

    # The assertion is simply that this returns.
    logging_config.configure_logging()


def test_stderr_logging_survives_an_unwritable_log_file(tmp_path, monkeypatch):
    """
    Falling back is only useful if logs still go somewhere. Losing the file and
    the stream would leave an operator debugging a silent container.
    """
    log_file = tmp_path / "logs" / "app.jsonl"
    log_file.parent.mkdir()

    monkeypatch.setattr(logging_config, "_resolve_log_file", lambda: log_file)
    monkeypatch.setattr(
        logging.handlers,
        "TimedRotatingFileHandler",
        lambda *a, **k: (_ for _ in ()).throw(OSError(30, "Read-only file system")),
    )

    logging_config.configure_logging()

    root = logging.getLogger()

    # TimedRotatingFileHandler subclasses StreamHandler, and the patch above
    # replaced the class anyway, so identify handlers by where they write
    # rather than by type.
    assert any(getattr(h, "stream", None) is sys.stderr for h in root.handlers), (
        "stderr logging must remain after the file handler is refused"
    )
    assert not any(hasattr(h, "baseFilename") for h in root.handlers), (
        "no file handler should be attached when the file could not be opened"
    )
    assert not log_file.exists(), "nothing should have been written to the refused path"


def test_writable_log_file_still_gets_a_file_handler(tmp_path, monkeypatch):
    """The fallback must not fire when the path is perfectly usable."""
    log_file = tmp_path / "logs" / "app.jsonl"
    monkeypatch.setattr(logging_config, "_resolve_log_file", lambda: log_file)

    logging_config.configure_logging()

    root = logging.getLogger()
    file_handlers = [
        h for h in root.handlers if isinstance(h, logging.handlers.TimedRotatingFileHandler)
    ]
    assert file_handlers, "a writable path must still produce a file handler"
    assert Path(file_handlers[0].baseFilename) == log_file

    for h in file_handlers:
        h.close()
