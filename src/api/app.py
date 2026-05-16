"""
FastAPI application factory.

Builds the Memory Vault REST API with routers, middleware, CORS,
rate limiting, and lifespan-managed database pool.
"""

from __future__ import annotations

import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path

import psycopg
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from src.api.deps import RateLimitMiddleware
from src.api.middleware import RequestIDMiddleware
from src.api.routers import chat, chunks, graph, health, ingest, search, spaces
from src.logging_config import configure_logging
from src.models.db import close_pool, init_pool

configure_logging()
logger = logging.getLogger(__name__)


@asynccontextmanager
async def _lifespan(app: FastAPI):
    await init_pool(min_size=2, max_size=10)
    logger.info("API started — database pool ready")
    try:
        yield
    finally:
        await close_pool()
        logger.info("API stopped — database pool closed")


def create_app() -> FastAPI:
    app = FastAPI(
        title="Memory Vault API",
        description=(
            "Local-first AI memory system — hybrid search, ingestion, "
            "and management for your personal memory store."
        ),
        version="0.4.0",
        lifespan=_lifespan,
        docs_url="/docs",
        redoc_url="/redoc",
    )

    origins = _parse_cors_origins(os.getenv("API_CORS_ORIGINS", "*"))
    app.add_middleware(
        CORSMiddleware,
        allow_origins=origins,
        allow_credentials=False,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    rate_limit = int(os.getenv("API_RATE_LIMIT_PER_MIN", "120"))
    app.add_middleware(RateLimitMiddleware, requests_per_minute=rate_limit)
    app.add_middleware(RequestIDMiddleware)

    @app.exception_handler(psycopg.OperationalError)
    async def _db_unavailable(_request: Request, exc: psycopg.OperationalError):
        logger.warning("Database unavailable: %s", exc)
        return JSONResponse(
            status_code=503,
            content={
                "detail": ("Database temporarily unavailable. Please retry shortly."),
            },
            headers={"Retry-After": "5"},
        )

    @app.exception_handler(Exception)
    async def _unhandled(_request: Request, exc: Exception):
        # Let FastAPI handle HTTPException / RequestValidationError naturally —
        # this is the catch-all that prevents stack traces leaking on 500s.
        if isinstance(exc, HTTPException):
            raise exc
        logger.exception("Unhandled error in request")
        return JSONResponse(
            status_code=500,
            content={"detail": "Internal server error. Check server logs."},
        )

    app.include_router(health.router)
    app.include_router(search.router)
    app.include_router(chunks.router)
    app.include_router(spaces.router)
    app.include_router(ingest.router)
    app.include_router(graph.router)
    app.include_router(chat.router)

    static_dir = Path(__file__).parent / "static"
    index_file = static_dir / "index.html"
    if static_dir.exists() and index_file.exists():
        app.mount(
            "/assets",
            StaticFiles(directory=str(static_dir / "assets")),
            name="assets",
        )

        @app.get("/{full_path:path}", include_in_schema=False)
        async def spa_fallback(request: Request, full_path: str):
            """Serve the React bundle for any non-API path (SPA deep-link support)."""
            if full_path.startswith(("api/", "docs", "redoc", "openapi.json")):
                raise HTTPException(status_code=404)
            # Security-critical: do not bypass _safe_static_path. It blocks
            # path-traversal (`../../etc/passwd`) on this unauthenticated route.
            candidate = _safe_static_path(static_dir, full_path)
            if not full_path or candidate is None or not candidate.is_file():
                return FileResponse(index_file)
            return FileResponse(candidate)

    return app


def _safe_static_path(static_root: Path, full_path: str) -> Path | None:
    """
    Resolve `full_path` against `static_root` and return the resolved path
    only if it stays within `static_root`. Returns None on traversal
    attempts (e.g. '../../etc/passwd') so the caller can fall back to
    serving the SPA shell.

    Security-critical: this is the only guard between the unauthenticated
    SPA fallback route and arbitrary file reads on the host filesystem.
    Do not bypass.

    Defense in depth — three layers, each sufficient on its own:
      1. Reject empty / null-byte / leading-slash inputs.
      2. Reject explicit traversal segments before path composition.
      3. After resolution, enforce that the candidate stays inside the
         trusted root via os.path.commonpath (the sanitizer pattern
         recognized by CodeQL's py/path-injection query).
    """
    if not full_path or "\x00" in full_path:
        return None
    # Strip leading slashes/backslashes so absolute user input cannot
    # override the trusted root on any platform.
    normalized = full_path.lstrip("/\\")
    requested = Path(normalized)
    if any(part in {"", ".", ".."} for part in requested.parts):
        return None
    try:
        root_real = os.path.realpath(static_root)
        candidate_real = os.path.realpath(os.path.join(root_real, normalized))
    except (ValueError, OSError):
        return None
    if os.path.commonpath([root_real, candidate_real]) != root_real:
        return None
    return Path(candidate_real)


def _parse_cors_origins(value: str) -> list[str]:
    value = value.strip()
    if not value or value == "*":
        return ["*"]
    return [o.strip() for o in value.split(",") if o.strip()]
