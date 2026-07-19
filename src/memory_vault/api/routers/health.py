"""Health check endpoint — no auth required."""

from __future__ import annotations

from fastapi import APIRouter

from memory_vault.api.schemas import HealthResponse
from memory_vault.models.db import health_check
from memory_vault.services.embedding import MODEL_NAME

router = APIRouter(prefix="/api", tags=["health"])


@router.get("/health", response_model=HealthResponse)
async def get_health() -> HealthResponse:
    """Return API and database health status."""
    db = await health_check()
    return HealthResponse(
        status="ok" if db["status"] == "healthy" else "degraded",
        database="connected" if db["status"] == "healthy" else "error",
        embedding_model=MODEL_NAME,
        version="0.4.0",
    )
