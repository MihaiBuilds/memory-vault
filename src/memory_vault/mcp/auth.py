"""Bearer-token verification for the MCP transport.

The MCP SDK authenticates with a `TokenVerifier`: one async method that turns a
bearer token into an `AccessToken`, or returns None to refuse it. This adapts
the same lookup the REST API uses, so both surfaces read one table through one
implementation and cannot drift.
"""

from __future__ import annotations

import logging

from mcp.server.auth.provider import AccessToken

from memory_vault.api.deps import look_up_token, mark_token_used

logger = logging.getLogger(__name__)

# MV's tokens are all-or-nothing: `api_tokens` has no scope column and every
# token that works at all can call every tool. Returning one descriptive scope
# is honest about that, where inventing a set of finer-grained names would
# imply a permission model that does not exist and cannot be enforced.
MCP_SCOPE = "memory-vault"

# A synthetic client id. OAuth flows use this to identify the application that
# obtained a token; MV's tokens are issued by an operator with
# `memory-vault token create`, so no client ever registers. The field is
# required by AccessToken, so it names the surface instead.
_CLIENT_ID = "memory-vault-mcp"


class DatabaseTokenVerifier:
    """Verify MCP bearer tokens against the `api_tokens` table.

    Structural implementation of the SDK's `TokenVerifier` protocol — it is a
    `Protocol`, not a base class, so this deliberately does not inherit from it.

    **`API_AUTH_ENABLED` is not consulted, and that is the point.** The REST
    API honours that flag as a local-dev convenience, but this transport is
    reachable over the network. An escape hatch that turns off authentication
    on a listening port is how a memory store ends up public, so the flag stops
    at the REST boundary.

    `expires_at` is deliberately left unset on the returned token. The SDK
    re-checks it against the server's wall clock if present, and MV already
    refuses expired tokens authoritatively against Postgres `now()`. Two clocks
    disagreeing about whether a credential is live is a bug waiting to happen;
    one gate is better than two that can diverge.
    """

    async def verify_token(self, token: str) -> AccessToken | None:
        """Return access info for a valid token, or None to refuse it.

        Every refusal returns None. The REST API distinguishes "expired" from
        "invalid" because that message reaches someone holding a real
        credential, but this protocol has no way to carry a reason — and a
        network client learning *why* a token failed tells an attacker whether
        a token ever existed.

        Never raises. An exception inside an auth check is how a transport
        ends up failing open, so an unexpected error is logged and refused.
        """
        try:
            result = await look_up_token(token)
        except Exception:
            logger.exception("MCP token verification failed; refusing the request")
            return None

        if not result.is_valid:
            logger.info("MCP token refused: %s", result.status.value)
            return None

        # Recording use is best-effort. Failing to update `last_used_at` is a
        # bookkeeping problem, not a reason to reject a credential that the
        # database just confirmed is good.
        try:
            assert result.token_id is not None  # nosec B101 — VALID carries an id
            await mark_token_used(result.token_id)
        except Exception:
            logger.warning("Could not record MCP token use", exc_info=True)

        return AccessToken(
            token=token,
            client_id=_CLIENT_ID,
            scopes=[MCP_SCOPE],
        )
