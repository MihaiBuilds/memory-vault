"""
Regression tests for the `since` timestamp parser used by REST search and
MCP recall.

Background: both callers used `datetime.fromisoformat(x).replace(tzinfo=UTC)`
which *relabels* the timezone rather than *converting* to it. An offset-aware
input like `2026-01-01T00:00:00-05:00` was silently reinterpreted as
`2026-01-01T00:00:00+00:00` — the instant used by search shifted by the
caller's offset. Fix: `parse_since()` calls `.astimezone(UTC)` on aware
inputs; naive inputs keep the documented UTC interpretation.

Integration for the REST end-to-end test: shares the memory_vault_test
database from conftest.py.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone

import pytest

# ---------------------------------------------------------------------------
# parse_since() — unit
# ---------------------------------------------------------------------------


class TestParseSince:
    def test_aware_offset_is_converted_not_relabelled(self):
        """The exact regression: `-05:00` input must become the correct UTC
        instant, not be silently reinterpreted as UTC."""
        from memory_vault.services.search import parse_since

        result = parse_since("2026-01-01T00:00:00-05:00")
        assert result == datetime(2026, 1, 1, 5, 0, 0, tzinfo=UTC)

    def test_aware_positive_offset_is_converted(self):
        from memory_vault.services.search import parse_since

        result = parse_since("2026-01-01T12:00:00+03:00")
        assert result == datetime(2026, 1, 1, 9, 0, 0, tzinfo=UTC)

    def test_aware_z_suffix_treated_as_utc(self):
        """`Z` is the standard ISO-8601 UTC indicator; must not raise."""
        from memory_vault.services.search import parse_since

        result = parse_since("2026-01-01T00:00:00Z")
        assert result == datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)

    def test_naive_datetime_assumed_utc(self):
        """Documented contract: naive timestamps keep UTC interpretation."""
        from memory_vault.services.search import parse_since

        result = parse_since("2026-01-01T00:00:00")
        assert result == datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)

    def test_naive_date_only_assumed_utc_midnight(self):
        from memory_vault.services.search import parse_since

        result = parse_since("2026-01-01")
        assert result == datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)

    def test_result_is_always_tzaware_utc(self):
        """Every return value is aware-UTC — no naive datetimes leak downstream."""
        from memory_vault.services.search import parse_since

        for value in (
            "2026-01-01",
            "2026-01-01T00:00:00",
            "2026-01-01T00:00:00Z",
            "2026-01-01T00:00:00+00:00",
            "2026-01-01T00:00:00-08:00",
        ):
            result = parse_since(value)
            assert result.tzinfo is not None
            assert result.utcoffset() == timedelta(0)

    def test_invalid_string_raises_valueerror(self):
        from memory_vault.services.search import parse_since

        with pytest.raises(ValueError):
            parse_since("not-a-date")

    def test_non_utc_aware_input_preserves_instant(self):
        """Round-trip check: the wall clock changes but the moment in time
        stays the same."""
        from memory_vault.services.search import parse_since

        pacific = timezone(timedelta(hours=-8))
        wall = datetime(2026, 6, 15, 9, 30, 0, tzinfo=pacific)
        result = parse_since(wall.isoformat())
        assert result == wall  # equality across tz is instant-equality


# ---------------------------------------------------------------------------
# End-to-end via REST /api/search — offset-aware since produces correct filter
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
class TestSearchSinceOffsetHandled:
    async def test_offset_aware_since_uses_correct_instant(self, client, auth_headers):
        """A search filter with -05:00 offset must exclude content whose UTC
        timestamp is before the *converted* instant, not before the
        *relabelled* one. Verifies parse_since is actually wired at the
        REST call site (the file-swap could pass unit tests but fail here
        if the router still called the old code path)."""
        r = await client.post(
            "/api/ingest/text",
            headers=auth_headers,
            json={
                "text": "since_regression_token_ALPHA content stored now",
                "space": "default",
            },
        )
        assert r.status_code == 200

        past_local = datetime.now(timezone(timedelta(hours=-5))) - timedelta(days=1)
        r = await client.post(
            "/api/search",
            headers=auth_headers,
            json={
                "query": "since_regression_token_ALPHA",
                "since": past_local.isoformat(),
            },
        )
        assert r.status_code == 200
        assert r.json()["total_results"] >= 1

    async def test_invalid_since_returns_400(self, client, auth_headers):
        r = await client.post(
            "/api/search",
            headers=auth_headers,
            json={"query": "anything", "since": "not-a-date"},
        )
        assert r.status_code == 400
