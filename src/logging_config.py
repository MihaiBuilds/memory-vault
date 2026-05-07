"""
Structured JSON logging for Memory Vault.

Single configure_logging() call wires up:
  - structlog with JSON renderer
  - stdlib logging captured into the same pipeline (so FastAPI / psycopg / etc.
    end up as JSON too, not text)
  - request-ID contextvar that auto-binds to every log record emitted during
    a given HTTP request (FastAPI middleware lives in src/api/middleware.py)
  - rotating file handler at LOG_FILE (default: /var/log/memory-vault/app.jsonl
    in Docker, ./logs/app.jsonl elsewhere)

Privacy rule: log identifiers (chunk_id, space_id, request_id, user_id),
never user-supplied content. The diagnose CLI also runs a redaction sweep
over the file at bundle time as a safety net.
"""

from __future__ import annotations

import logging
import logging.handlers
import os
import sys
from contextvars import ContextVar
from pathlib import Path

import structlog

request_id_var: ContextVar[str | None] = ContextVar("request_id", default=None)


def _add_request_id(_, __, event_dict: dict) -> dict:
    rid = request_id_var.get()
    if rid is not None:
        event_dict["request_id"] = rid
    return event_dict


def _resolve_log_file() -> Path:
    explicit = os.environ.get("LOG_FILE")
    if explicit:
        return Path(explicit)
    if Path("/.dockerenv").exists():
        return Path("/var/log/memory-vault/app.jsonl")
    return Path.cwd() / "logs" / "app.jsonl"


def configure_logging() -> None:
    level_name = os.environ.get("LOG_LEVEL", "INFO").upper()
    level = getattr(logging, level_name, logging.INFO)

    log_file = _resolve_log_file()
    try:
        log_file.parent.mkdir(parents=True, exist_ok=True)
    except (PermissionError, OSError):
        # Read-only filesystem or no write access — fall back to stderr only.
        log_file = None

    shared_processors: list = [
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        _add_request_id,
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
    ]

    structlog.configure(
        processors=shared_processors
        + [
            structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
        ],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.make_filtering_bound_logger(level),
        cache_logger_on_first_use=True,
    )

    formatter = structlog.stdlib.ProcessorFormatter(
        foreign_pre_chain=shared_processors,
        processors=[
            structlog.stdlib.ProcessorFormatter.remove_processors_meta,
            structlog.processors.JSONRenderer(),
        ],
    )

    root = logging.getLogger()
    root.handlers.clear()
    root.setLevel(level)

    stderr_handler = logging.StreamHandler(stream=sys.stderr)
    stderr_handler.setFormatter(formatter)
    root.addHandler(stderr_handler)

    if log_file is not None:
        file_handler = logging.handlers.TimedRotatingFileHandler(
            log_file,
            when="midnight",
            backupCount=7,
            encoding="utf-8",
        )
        file_handler.setFormatter(formatter)
        root.addHandler(file_handler)

    # Quiet noisy third-party loggers at INFO; respect LOG_LEVEL=DEBUG override.
    if level > logging.DEBUG:
        for noisy in ("httpx", "httpcore", "urllib3", "asyncio"):
            logging.getLogger(noisy).setLevel(logging.WARNING)


def get_log_file() -> Path | None:
    """Used by the diagnose CLI to locate the log file for bundling."""
    candidate = _resolve_log_file()
    return candidate if candidate.exists() else None
