"""Health check endpoint — no auth required."""

from __future__ import annotations

from fastapi import APIRouter, Response, status

from memory_vault import __version__
from memory_vault.api.schemas import HealthResponse
from memory_vault.models.db import health_check
from memory_vault.services.embedding import MODEL_NAME

router = APIRouter(prefix="/api", tags=["health"])


@router.get("/health", response_model=HealthResponse)
async def get_health(response: Response) -> HealthResponse:
    """Return API and database health status.

    Returns HTTP 200 when the database is reachable, HTTP 503 when it is not.
    The body always carries the same shape (embedding_model, version) so
    operators curling the endpoint keep the debug context regardless of
    outcome. The Docker HEALTHCHECK relies on the status code alone.
    """
    db = await health_check()
    healthy = db["status"] == "healthy"
    if not healthy:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    return HealthResponse(
        status="ok" if healthy else "degraded",
        database="connected" if healthy else "error",
        embedding_model=MODEL_NAME,
        version=__version__,
    )
