"""
The shared token lookup.

`require_token` used to do the table scan itself. The MCP transport needs the
same check from a `TokenVerifier`, and two auth paths over one table drift —
the one that drifts being the one nobody is looking at. So the lookup moved
out, and both surfaces call it.

What makes this refactor worth testing directly rather than through the REST
endpoint: the lookup now reports *why* a token failed, and the two callers
make different decisions from that. `require_token` turns UNKNOWN and REVOKED
into one message and EXPIRED into another; the MCP verifier will simply refuse
anything that is not VALID. Neither can be checked through the other.

`test_token_expiry.py` remains the regression net for the HTTP behaviour —
these tests are about the function underneath it.
"""

from __future__ import annotations

import pytest

from memory_vault.api.deps import (
    TokenLookup,
    TokenStatus,
    create_token,
    hash_token,
    look_up_token,
    mark_token_used,
    revoke_token,
)
from memory_vault.models.db import execute_query, fetch_one


class TestTheFourOutcomes:
    async def test_a_live_token_is_valid(self):
        token = await create_token("lookup-live")

        result = await look_up_token(token)

        assert result.status is TokenStatus.VALID
        assert result.is_valid
        assert result.token_id is not None

    async def test_a_token_that_never_existed_is_unknown(self):
        result = await look_up_token("mv_this-was-never-issued")

        assert result.status is TokenStatus.UNKNOWN
        assert result.token_id is None, "there is no row to point at"

    async def test_a_revoked_token_is_revoked(self):
        token = await create_token("lookup-revoked")
        prefix = token[:11]
        assert await revoke_token(prefix) is True

        result = await look_up_token(token)

        assert result.status is TokenStatus.REVOKED
        assert result.token_id is not None

    async def test_an_expired_token_is_expired(self):
        token = await create_token("lookup-expired", expires_in_days=1)
        await execute_query(
            "UPDATE api_tokens SET expires_at = now() - interval '1 hour' WHERE token_prefix = %s",
            (token[:11],),
        )

        result = await look_up_token(token)

        assert result.status is TokenStatus.EXPIRED
        assert result.token_id is not None


class TestTellingDeadTokensApart:
    """
    The reason expired and revoked rows are fetched rather than filtered out in
    SQL. An anonymous caller never learns the difference — `require_token`
    collapses UNKNOWN and REVOKED into one message — but the holder of a real
    credential gets told which of the two happened, which saves an operator
    hunting a bug that is really a lapsed token.
    """

    async def test_a_dead_token_still_reports_its_row(self):
        token = await create_token("lookup-dead")
        await revoke_token(token[:11])

        result = await look_up_token(token)

        assert result.token_id is not None, (
            "a token that once existed must be distinguishable from one that never did"
        )

    async def test_revoked_beats_expired_when_both_apply(self):
        """
        Order matters: reporting a revoked token as merely expired would
        suggest renewing it might help, which it would not.
        """
        token = await create_token("lookup-both", expires_in_days=1)
        await execute_query(
            "UPDATE api_tokens SET expires_at = now() - interval '1 hour', "
            "revoked_at = now() WHERE token_prefix = %s",
            (token[:11],),
        )

        result = await look_up_token(token)

        assert result.status is TokenStatus.REVOKED


class TestItIsPolicyFree:
    """
    The lookup deliberately says nothing about `API_AUTH_ENABLED`. Whether a
    surface may skip auth is that surface's decision: REST honours the flag as
    a local-dev convenience, and the MCP transport — being network-reachable —
    will not. Baking the flag in here would silently hand the escape hatch to
    every future caller.
    """

    async def test_the_flag_does_not_change_the_answer(self, monkeypatch):
        token = await create_token("lookup-policy")

        monkeypatch.setenv("API_AUTH_ENABLED", "false")
        with_auth_off = await look_up_token(token)
        monkeypatch.setenv("API_AUTH_ENABLED", "true")
        with_auth_on = await look_up_token(token)

        assert with_auth_off.status is TokenStatus.VALID
        assert with_auth_on.status is TokenStatus.VALID

    async def test_an_unknown_token_is_unknown_even_with_auth_disabled(self, monkeypatch):
        monkeypatch.setenv("API_AUTH_ENABLED", "false")

        result = await look_up_token("mv_still-not-a-token")

        assert result.status is TokenStatus.UNKNOWN, (
            "disabling auth is a decision for the caller, not something the "
            "lookup should apply on its behalf"
        )


class TestMarkingUse:
    async def test_it_records_when_a_token_was_used(self):
        token = await create_token("lookup-used")
        result = await look_up_token(token)

        before = await fetch_one(
            "SELECT last_used_at FROM api_tokens WHERE id = %s", (result.token_id,)
        )
        assert before["last_used_at"] is None, "precondition: never used yet"

        await mark_token_used(result.token_id)

        after = await fetch_one(
            "SELECT last_used_at FROM api_tokens WHERE id = %s", (result.token_id,)
        )
        assert after["last_used_at"] is not None

    async def test_the_lookup_itself_does_not_write(self):
        """
        Kept separate because the lookup is a read. A caller that only wants to
        know whether a token is good — a health check, a verifier deciding
        whether to accept a connection — should not leave a trace.
        """
        token = await create_token("lookup-readonly")

        result = await look_up_token(token)

        row = await fetch_one(
            "SELECT last_used_at FROM api_tokens WHERE id = %s", (result.token_id,)
        )
        assert row["last_used_at"] is None, "looking a token up must not mark it used"


class TestTheHashIsWhatIsCompared:
    async def test_the_plaintext_is_never_stored(self):
        token = await create_token("lookup-hashing")

        row = await fetch_one(
            "SELECT token_hash FROM api_tokens WHERE token_prefix = %s", (token[:11],)
        )

        assert row["token_hash"] == hash_token(token)
        assert token not in row["token_hash"]

    @pytest.mark.parametrize(
        "bad", ["", " ", "not-a-token", "mv_", "MV_WRONG_CASE", "'; DROP TABLE api_tokens; --"]
    )
    async def test_malformed_input_is_refused_without_error(self, bad):
        """
        A verifier will be handed whatever a client sends. Anything unparseable
        must come back UNKNOWN rather than raising — an exception inside an
        auth check is how a transport ends up failing open.
        """
        result = await look_up_token(bad)

        assert result.status is TokenStatus.UNKNOWN

        still_there = await fetch_one("SELECT to_regclass('public.api_tokens') AS t")
        assert still_there["t"] is not None


class TestTheResultShape:
    def test_is_valid_tracks_the_status(self):
        assert TokenLookup(TokenStatus.VALID, "x").is_valid is True
        for dead in (TokenStatus.UNKNOWN, TokenStatus.REVOKED, TokenStatus.EXPIRED):
            assert TokenLookup(dead, "x").is_valid is False, dead

    def test_the_result_is_immutable(self):
        """It is an auth decision — nothing downstream should be able to edit it."""
        result = TokenLookup(TokenStatus.UNKNOWN)

        with pytest.raises(Exception):  # noqa: B017 - FrozenInstanceError
            result.status = TokenStatus.VALID  # type: ignore[misc]
