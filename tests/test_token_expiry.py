"""
Token expiry.

A token used to be valid from creation until someone revoked it by hand.
`expires_at` makes that optional per token, and the auth path has to
distinguish three states that all end in 401 for the caller: a token that
never existed, one that was revoked, and one that simply lapsed. An operator
debugging a 401 needs to know which.
"""

from __future__ import annotations

import pytest

from memory_vault.api.deps import create_token, revoke_token
from memory_vault.models.db import execute_query, fetch_one

pytestmark = pytest.mark.asyncio


async def _set_expiry(token_prefix: str, sql_interval: str) -> None:
    """Move a token's expiry relative to now(), e.g. '-1 day' or '+1 day'."""
    await execute_query(
        f"UPDATE api_tokens SET expires_at = now() + interval '{sql_interval}' "  # nosec B608
        "WHERE token_prefix = %s",
        (token_prefix,),
    )


class TestTokenExpiry:
    async def test_token_without_expiry_is_accepted(self, client):
        """The default and the pre-existing behaviour: no expiry means never."""
        token = await create_token("no-expiry")

        row = await fetch_one(
            "SELECT expires_at FROM api_tokens WHERE token_prefix = %s", (token[:11],)
        )
        assert row["expires_at"] is None, "a token created without an expiry must not have one"

        r = await client.get("/api/spaces", headers={"Authorization": f"Bearer {token}"})
        assert r.status_code == 200

    async def test_unexpired_token_is_accepted(self, client):
        token = await create_token("future-expiry", expires_in_days=30)

        row = await fetch_one(
            "SELECT expires_at FROM api_tokens WHERE token_prefix = %s", (token[:11],)
        )
        assert row["expires_at"] is not None, "expires_in_days must set expires_at"

        r = await client.get("/api/spaces", headers={"Authorization": f"Bearer {token}"})
        assert r.status_code == 200

    async def test_expired_token_returns_401(self, client):
        token = await create_token("lapsed", expires_in_days=1)
        await _set_expiry(token[:11], "-1 day")

        r = await client.get("/api/spaces", headers={"Authorization": f"Bearer {token}"})
        assert r.status_code == 401

    async def test_expiry_boundary_is_exclusive(self, client):
        """
        `expires_at <= now()` — a token whose expiry is exactly now is spent.

        Set it a hair in the past rather than exactly now(), because the row is
        written by one statement and read by another: an exact now() would be
        microseconds in the past by the time auth reads it, which is what the
        assertion is really about.
        """
        token = await create_token("boundary", expires_in_days=1)
        await _set_expiry(token[:11], "-1 microsecond")

        r = await client.get("/api/spaces", headers={"Authorization": f"Bearer {token}"})
        assert r.status_code == 401

    async def test_expired_and_revoked_are_told_apart(self, client):
        """
        Both are 401, but the messages must differ. This is the whole reason
        the auth query stopped filtering dead tokens out in SQL.
        """
        expired = await create_token("expired-msg", expires_in_days=1)
        await _set_expiry(expired[:11], "-1 day")

        revoked = await create_token("revoked-msg")
        assert await revoke_token(revoked[:11]) is True

        r_expired = await client.get("/api/spaces", headers={"Authorization": f"Bearer {expired}"})
        r_revoked = await client.get("/api/spaces", headers={"Authorization": f"Bearer {revoked}"})

        assert r_expired.status_code == 401
        assert r_revoked.status_code == 401
        assert r_expired.json()["detail"] != r_revoked.json()["detail"], (
            "an operator debugging a 401 must be able to tell expiry from revocation"
        )
        assert "expired" in r_expired.json()["detail"].lower()

    async def test_unknown_token_matches_the_revoked_message(self, client):
        """
        A token that never existed must not be distinguishable from one that
        was revoked — otherwise the error text confirms which random strings
        were once real tokens.
        """
        revoked = await create_token("probe")
        assert await revoke_token(revoked[:11]) is True

        r_revoked = await client.get("/api/spaces", headers={"Authorization": f"Bearer {revoked}"})
        r_unknown = await client.get(
            "/api/spaces", headers={"Authorization": "Bearer mv_never_existed"}
        )

        assert r_revoked.status_code == r_unknown.status_code == 401
        assert r_revoked.json()["detail"] == r_unknown.json()["detail"]

    async def test_revoked_beats_expired_when_both_apply(self, client):
        """Revocation is a decision; expiry is time passing. The decision wins."""
        token = await create_token("both")
        await _set_expiry(token[:11], "-1 day")
        assert await revoke_token(token[:11]) is True

        r = await client.get("/api/spaces", headers={"Authorization": f"Bearer {token}"})
        assert r.status_code == 401
        assert "revoked" in r.json()["detail"].lower()


@pytest.mark.parametrize("days", [1, 7, 365])
async def test_expires_in_days_lands_in_the_future(days):
    """The interval is applied in SQL; check the arithmetic actually happens."""
    token = await create_token(f"span-{days}", expires_in_days=days)
    row = await fetch_one(
        """SELECT expires_at,
                  (expires_at > now()) AS in_future,
                  (expires_at < now() + make_interval(days => %s) + interval '1 minute')
                      AS not_overshot
           FROM api_tokens WHERE token_prefix = %s""",
        (days, token[:11]),
    )
    assert row["in_future"] is True
    assert row["not_overshot"] is True
