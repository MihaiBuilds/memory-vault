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
from dataclasses import dataclass
from enum import StrEnum

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


async def create_token(name: str, expires_in_days: int | None = None) -> str:
    """
    Persist a new token and return the plaintext (shown once).

    `expires_in_days=None` creates a token that never expires, which is the
    default and matches every token issued before expiry existed.
    """
    plaintext, token_hash, prefix = generate_token()
    await execute_query(
        """INSERT INTO api_tokens (name, token_hash, token_prefix, expires_at)
           VALUES (%s, %s, %s,
                   CASE WHEN %s::int IS NULL THEN NULL
                        ELSE now() + make_interval(days => %s::int) END)""",
        (name, token_hash, prefix, expires_in_days, expires_in_days),
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


class TokenStatus(StrEnum):
    """Why a presented token was accepted or refused."""

    VALID = "valid"
    UNKNOWN = "unknown"
    REVOKED = "revoked"
    EXPIRED = "expired"


@dataclass(frozen=True)
class TokenLookup:
    """The outcome of checking one presented token.

    `token_id` is set only when a row matched, whatever its state — so a
    caller can tell a lapsed credential from one that never existed, which is
    the difference between "your token expired" and "invalid token".
    """

    status: TokenStatus
    token_id: str | None = None

    @property
    def is_valid(self) -> bool:
        return self.status is TokenStatus.VALID


async def look_up_token(presented: str) -> TokenLookup:
    """Check a plaintext token against the `api_tokens` table.

    Extracted from `require_token` so the REST dependency and the MCP
    transport's `TokenVerifier` share one implementation. Two auth paths over
    the same table would drift — and the one that drifts is the one nobody is
    looking at.

    Says nothing about `API_AUTH_ENABLED`. Whether auth can be skipped at all
    is a policy decision belonging to each surface: the REST API honours the
    flag as a local-dev convenience, while the MCP transport is
    network-reachable and never does.

    Expired and revoked rows are fetched rather than filtered out in SQL, so a
    token that once existed can be told apart from one that never did. That
    distinction never reaches an anonymous caller — it only changes which
    message the holder of a real-but-dead token sees.
    """
    presented_hash = hash_token(presented)

    rows = await fetch_all(
        """SELECT id, revoked_at,
                  (expires_at IS NOT NULL AND expires_at <= now()) AS is_expired,
                  token_hash
           FROM api_tokens""",
    )

    # Constant-time scan over all tokens. SHA-256 of a 32-byte token gives
    # effectively-random hashes, so a non-constant-time SQL `=` lookup would
    # already be hard to time-attack — but compare_digest makes the property
    # explicit and satisfies the v1.0 security review verbatim.
    matched = None
    for row in rows:
        if hmac.compare_digest(presented_hash, row["token_hash"]):
            matched = row
            break

    if matched is None:
        return TokenLookup(TokenStatus.UNKNOWN)

    token_id = str(matched["id"])

    # Revoked beats expired: a revoked token is refused on purpose, and
    # reporting it as merely expired would suggest renewing it would help.
    if matched["revoked_at"] is not None:
        return TokenLookup(TokenStatus.REVOKED, token_id)

    if matched["is_expired"]:
        return TokenLookup(TokenStatus.EXPIRED, token_id)

    return TokenLookup(TokenStatus.VALID, token_id)


async def mark_token_used(token_id: str) -> None:
    """Record that a token was just accepted.

    Separate from the lookup because it is a write, and a caller that only
    wants to know whether a token is good should not have to perform one.
    """
    await execute_query(
        "UPDATE api_tokens SET last_used_at = now() WHERE id = %s",
        (token_id,),
    )


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

    result = await look_up_token(credentials.credentials)

    # Unknown and revoked share a message on purpose: an anonymous caller
    # learns nothing about whether a token ever existed. Expiry is told apart
    # because that message only reaches someone holding a real credential, and
    # it saves an operator from hunting a bug that is really a lapsed token.
    if result.status in (TokenStatus.UNKNOWN, TokenStatus.REVOKED):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or revoked token",
            headers={"WWW-Authenticate": "Bearer"},
        )

    if result.status is TokenStatus.EXPIRED:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Token expired",
            headers={"WWW-Authenticate": "Bearer"},
        )

    assert result.token_id is not None  # nosec B101 — VALID always carries an id
    await mark_token_used(result.token_id)


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
