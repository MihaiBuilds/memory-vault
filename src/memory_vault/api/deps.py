"""
Shared dependencies and middleware:
  - Bearer token authentication (checks api_tokens table)
  - In-memory rate limiting (per-client, per-minute sliding window)
  - Token generation and hashing helpers
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import os
import secrets
import time
from collections import defaultdict, deque

from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse, Response

from memory_vault.models.db import execute_query, fetch_all

logger = logging.getLogger(__name__)

_TOKEN_BYTES = 32
_bearer = HTTPBearer(auto_error=False)


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def generate_token() -> tuple[str, str, str]:
    """Create a new token. Returns (plaintext, hash, prefix)."""
    token = "mv_" + secrets.token_urlsafe(_TOKEN_BYTES)
    return token, hash_token(token), token[:11]


async def create_token(name: str) -> str:
    """Persist a new token and return the plaintext (shown once)."""
    plaintext, token_hash, prefix = generate_token()
    await execute_query(
        """INSERT INTO api_tokens (name, token_hash, token_prefix)
           VALUES (%s, %s, %s)""",
        (name, token_hash, prefix),
    )
    return plaintext


async def revoke_token(prefix: str) -> bool:
    rowcount = await execute_query(
        """UPDATE api_tokens
           SET revoked_at = now()
           WHERE token_prefix = %s AND revoked_at IS NULL""",
        (prefix,),
    )
    return rowcount > 0


def auth_enabled() -> bool:
    return os.getenv("API_AUTH_ENABLED", "true").lower() not in ("false", "0", "no")


async def require_token(
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer),
) -> None:
    """
    FastAPI dependency: validate the Authorization: Bearer <token> header.

    Auth is disabled when API_AUTH_ENABLED=false (useful for local dev
    and tests) and enabled by default otherwise.
    """
    if not auth_enabled():
        return

    if credentials is None or credentials.scheme.lower() != "bearer":
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing or invalid Authorization header",
            headers={"WWW-Authenticate": "Bearer"},
        )

    presented_hash = hash_token(credentials.credentials)
    rows = await fetch_all(
        """SELECT id, token_hash FROM api_tokens
           WHERE revoked_at IS NULL""",
    )

    # Constant-time scan over all active tokens. SHA-256 of a 32-byte token
    # gives effectively-random hashes, so a non-constant-time SQL `=` lookup
    # would already be hard to time-attack — but compare_digest makes the
    # property explicit and satisfies the v1.0 security review verbatim.
    matched_id = None
    for row in rows:
        if hmac.compare_digest(presented_hash, row["token_hash"]):
            matched_id = row["id"]
            break

    if matched_id is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or revoked token",
            headers={"WWW-Authenticate": "Bearer"},
        )

    await execute_query(
        "UPDATE api_tokens SET last_used_at = now() WHERE id = %s",
        (matched_id,),
    )


# ---------------------------------------------------------------------------
# Rate limiting
# ---------------------------------------------------------------------------


class RateLimitMiddleware(BaseHTTPMiddleware):
    """Sliding-window rate limit keyed by client IP."""

    def __init__(self, app, requests_per_minute: int = 120) -> None:
        super().__init__(app)
        self._limit = requests_per_minute
        self._window = 60.0
        self._hits: dict[str, deque[float]] = defaultdict(deque)

    async def dispatch(self, request: Request, call_next) -> Response:
        if request.url.path in ("/api/health", "/docs", "/redoc", "/openapi.json"):
            return await call_next(request)

        client = request.client.host if request.client else "unknown"
        now = time.monotonic()
        hits = self._hits[client]

        while hits and now - hits[0] > self._window:
            hits.popleft()

        if len(hits) >= self._limit:
            retry_after = int(self._window - (now - hits[0])) + 1
            return JSONResponse(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                content={"detail": "Rate limit exceeded"},
                headers={"Retry-After": str(retry_after)},
            )

        hits.append(now)
        return await call_next(request)
