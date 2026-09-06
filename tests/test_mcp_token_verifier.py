"""
Bearer-token verification for the MCP transport.

The SDK authenticates with a `TokenVerifier`: one async method that turns a
token into an `AccessToken`, or returns None to refuse it. This adapts the
lookup the REST API already uses, so both surfaces read one table through one
implementation.

Three properties are worth testing here rather than assuming, and each is
about what happens on the *refusal* path — the one that decides whether a
network-reachable memory store stays private:

**Every refusal is None.** The protocol carries no reason, and that suits the
surface: a network client learning why a token failed learns whether it ever
existed.

**`API_AUTH_ENABLED` is not consulted.** The REST API honours it as a
local-dev convenience. A flag that disables authentication on a listening
port is how an instance ends up public, so it stops at the REST boundary.

**Nothing raises.** An exception inside an auth check is how a transport ends
up failing open, so an unexpected error is logged and refused.
"""

from __future__ import annotations

import inspect

import pytest

from memory_vault.api.deps import create_token, revoke_token
from memory_vault.mcp.auth import MCP_SCOPE, DatabaseTokenVerifier
from memory_vault.models.db import execute_query, fetch_one


@pytest.fixture
def verifier() -> DatabaseTokenVerifier:
    return DatabaseTokenVerifier()


async def _expire(token: str) -> None:
    await execute_query(
        "UPDATE api_tokens SET expires_at = now() - interval '1 hour' WHERE token_prefix = %s",
        (token[:11],),
    )


class TestAcceptingAGoodToken:
    async def test_a_live_token_is_accepted(self, verifier):
        token = await create_token("mcp-live")

        result = await verifier.verify_token(token)

        assert result is not None
        assert result.token == token

    async def test_it_carries_the_scope_the_transport_expects(self, verifier):
        token = await create_token("mcp-scope")

        result = await verifier.verify_token(token)

        assert result.scopes == [MCP_SCOPE]

    async def test_expires_at_is_left_unset(self, verifier):
        """
        The SDK re-checks `expires_at` against the server's wall clock when it
        is present. MV already refuses expired tokens against Postgres `now()`,
        and two clocks disagreeing about whether a credential is live is a bug
        waiting to happen. One gate, not two that can diverge.
        """
        token = await create_token("mcp-noexp", expires_in_days=30)

        result = await verifier.verify_token(token)

        assert result.expires_at is None, (
            "populating this would add a second expiry check on a different clock"
        )

    async def test_use_is_recorded(self, verifier):
        token = await create_token("mcp-used")

        await verifier.verify_token(token)

        row = await fetch_one(
            "SELECT last_used_at FROM api_tokens WHERE token_prefix = %s", (token[:11],)
        )
        assert row["last_used_at"] is not None


class TestEveryRefusalIsNone:
    """
    The REST API tells "expired" apart from "invalid" because that message
    reaches someone holding a real credential. This protocol has no way to
    carry a reason, and on a network surface that is the better default: an
    attacker learns nothing about whether a token ever existed.
    """

    async def test_an_unknown_token_is_refused(self, verifier):
        assert await verifier.verify_token("mv_never-issued") is None

    async def test_a_revoked_token_is_refused(self, verifier):
        token = await create_token("mcp-revoked")
        await revoke_token(token[:11])

        assert await verifier.verify_token(token) is None

    async def test_an_expired_token_is_refused(self, verifier):
        token = await create_token("mcp-expired", expires_in_days=1)
        await _expire(token)

        assert await verifier.verify_token(token) is None

    @pytest.mark.parametrize(
        "junk",
        ["", " ", "not-a-token", "mv_", "Bearer mv_something", "'; DROP TABLE api_tokens; --"],
    )
    async def test_malformed_input_is_refused_without_raising(self, verifier, junk):
        """A verifier is handed whatever a client sends."""
        assert await verifier.verify_token(junk) is None

        still_there = await fetch_one("SELECT to_regclass('public.api_tokens') AS t")
        assert still_there["t"] is not None

    async def test_a_refused_token_is_not_marked_used(self, verifier):
        token = await create_token("mcp-refused")
        await revoke_token(token[:11])

        await verifier.verify_token(token)

        row = await fetch_one(
            "SELECT last_used_at FROM api_tokens WHERE token_prefix = %s", (token[:11],)
        )
        assert row["last_used_at"] is None


class TestTheAuthFlagStopsAtRest:
    """
    The decision this transport rests on. `API_AUTH_ENABLED=false` is a
    local-dev convenience for the REST API; applying it here would leave an
    unauthenticated memory store on a listening port.
    """

    async def test_an_unknown_token_is_still_refused_with_auth_disabled(
        self, verifier, monkeypatch
    ):
        monkeypatch.setenv("API_AUTH_ENABLED", "false")

        assert await verifier.verify_token("mv_still-not-a-token") is None, (
            "disabling REST auth must not open the MCP transport"
        )

    async def test_a_revoked_token_is_still_refused_with_auth_disabled(self, verifier, monkeypatch):
        token = await create_token("mcp-flag-revoked")
        await revoke_token(token[:11])
        monkeypatch.setenv("API_AUTH_ENABLED", "false")

        assert await verifier.verify_token(token) is None

    async def test_a_good_token_still_works_with_auth_disabled(self, verifier, monkeypatch):
        """The flag is ignored in both directions — it changes nothing here."""
        token = await create_token("mcp-flag-good")
        monkeypatch.setenv("API_AUTH_ENABLED", "false")

        assert await verifier.verify_token(token) is not None


class TestItNeverRaises:
    """
    An exception inside an auth check is how a transport ends up failing open.
    Whatever goes wrong underneath, the answer is a refusal.
    """

    async def test_a_lookup_failure_refuses_rather_than_propagating(self, verifier, monkeypatch):
        from memory_vault.mcp import auth as auth_mod

        async def _explode(_token):
            raise RuntimeError("database is on fire")

        monkeypatch.setattr(auth_mod, "look_up_token", _explode)

        assert await verifier.verify_token("mv_anything") is None

    async def test_a_bookkeeping_failure_does_not_reject_a_good_token(self, verifier, monkeypatch):
        """
        Recording use is best-effort. Failing to update `last_used_at` is a
        bookkeeping problem, not a reason to refuse a credential the database
        just confirmed is good.
        """
        from memory_vault.mcp import auth as auth_mod

        token = await create_token("mcp-bookkeeping")

        async def _explode(_token_id):
            raise RuntimeError("write failed")

        monkeypatch.setattr(auth_mod, "mark_token_used", _explode)

        assert await verifier.verify_token(token) is not None


class TestItFitsTheSdkProtocol:
    """
    `TokenVerifier` is a `Protocol` and is not `@runtime_checkable`, so
    `isinstance` raises rather than answering. Conformance is checked by shape
    instead, and — more usefully — by handing the adapter to the SDK's own
    middleware.
    """

    def test_the_method_matches_the_protocol_signature(self):
        from mcp.server.auth.provider import TokenVerifier

        expected = inspect.signature(TokenVerifier.verify_token)
        actual = inspect.signature(DatabaseTokenVerifier.verify_token)

        assert list(actual.parameters) == list(expected.parameters)
        assert inspect.iscoroutinefunction(DatabaseTokenVerifier.verify_token)

    async def test_the_sdk_middleware_accepts_it(self, verifier):
        """
        The real integration point: the SDK's bearer middleware authenticates
        a connection through whatever verifier it is given.
        """
        from mcp.server.auth.middleware.bearer_auth import BearerAuthBackend

        token = await create_token("mcp-middleware")
        backend = BearerAuthBackend(token_verifier=verifier)

        class _Conn:
            headers = {"authorization": f"Bearer {token}"}

        result = await backend.authenticate(_Conn())

        assert result is not None, "the SDK middleware should accept a valid MV token"
        credentials, user = result
        assert MCP_SCOPE in credentials.scopes

    async def test_the_sdk_middleware_rejects_a_bad_token(self, verifier):
        from mcp.server.auth.middleware.bearer_auth import BearerAuthBackend

        backend = BearerAuthBackend(token_verifier=verifier)

        class _Conn:
            headers = {"authorization": "Bearer mv_not-a-real-token"}

        assert await backend.authenticate(_Conn()) is None
