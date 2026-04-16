"""
FastAPI application factory.

Builds the Memory Vault REST API with routers, middleware, CORS,
rate limiting, and lifespan-managed database pool.
"""

from __future__ import annotations

import logging
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from pathlib import Path

from src.api.deps import RateLimitMiddleware
from src.api.routers import chunks, health, ingest, search, spaces
from src.models.db import close_pool, init_pool

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

    app.include_router(health.router)
    app.include_router(search.router)
    app.include_router(chunks.router)
    app.include_router(spaces.router)
    app.include_router(ingest.router)

    static_dir = Path(__file__).parent / "static"
    if static_dir.exists() and any(static_dir.iterdir()):
        app.mount("/", StaticFiles(directory=str(static_dir), html=True), name="static")

    return app


def _parse_cors_origins(value: str) -> list[str]:
    value = value.strip()
    if not value or value == "*":
        return ["*"]
    return [o.strip() for o in value.split(",") if o.strip()]
