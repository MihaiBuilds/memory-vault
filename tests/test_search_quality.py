"""Search-quality diagnostic — aggregate over what searching already recorded.

The two behaviours worth pinning down are both about what the average hides:
an empty search records NULL and is skipped by AVG, and a search that matches
nothing still returns rows, so weak matches are the signal that actually fires.
"""

from __future__ import annotations

import pytest

from memory_vault.models.db import execute_query
from memory_vault.services import search as search_service


async def _log(
    query_text: str,
    top_similarity: float | None,
    result_count: int = 5,
    *,
    age_hours: float = 0.0,
) -> None:
    """Insert a query_log row directly, optionally backdated."""
    await execute_query(
        """INSERT INTO query_log
               (query_text, result_count, top_similarity, latency_ms, created_at)
           VALUES (%s, %s, %s, %s, now() - make_interval(mins => %s))""",
        (query_text, result_count, top_similarity, 12, int(age_hours * 60)),
        commit=True,
    )


@pytest.mark.asyncio
async def test_reports_nothing_when_no_searches():
    quality = await search_service.recent_search_quality()

    assert quality.queries == 0
    # None, not 0.0 — an unmeasured vault must not read as a failing one.
    assert quality.avg_top_similarity is None
    assert quality.weak_matches == 0
    assert quality.empty_results == 0


@pytest.mark.asyncio
async def test_averages_the_best_hit_per_search():
    await _log("a", 0.60)
    await _log("b", 0.40)

    quality = await search_service.recent_search_quality()

    assert quality.queries == 2
    assert quality.avg_top_similarity == pytest.approx(0.50)


@pytest.mark.asyncio
async def test_empty_searches_are_counted_not_averaged():
    """A vault answering mostly nothing must not look healthy.

    top_similarity is NULL for a search that returned no rows, and AVG skips
    NULLs. Without a separate count, three empty searches and one good one
    would report the good one's score and nothing else.
    """
    await _log("found something", 0.80)
    for i in range(3):
        await _log(f"found nothing {i}", None, result_count=0)

    quality = await search_service.recent_search_quality()

    assert quality.queries == 4
    assert quality.empty_results == 3
    # The average still only reflects the one search that matched — which is
    # exactly why empty_results is reported alongside it rather than folded in.
    assert quality.avg_top_similarity == pytest.approx(0.80)


@pytest.mark.asyncio
async def test_counts_weak_matches_that_returned_rows():
    """The real failure mode: results came back, none of them were answers."""
    await _log("good", 0.70)
    await _log("weak one", 0.10)
    await _log("weak two", 0.20)

    quality = await search_service.recent_search_quality()

    assert quality.queries == 3
    assert quality.empty_results == 0  # every search returned rows
    assert quality.weak_matches == 2


@pytest.mark.asyncio
async def test_weak_threshold_boundary_is_exclusive():
    """A score exactly at the threshold is not weak."""
    await _log("exactly at", search_service.WEAK_MATCH_SIMILARITY)
    await _log("just below", search_service.WEAK_MATCH_SIMILARITY - 0.01)

    quality = await search_service.recent_search_quality()

    assert quality.weak_matches == 1


@pytest.mark.asyncio
async def test_ignores_searches_outside_the_window():
    await _log("recent", 0.90)
    await _log("stale", 0.10, age_hours=search_service.SEARCH_QUALITY_WINDOW_HOURS + 1)

    quality = await search_service.recent_search_quality()

    assert quality.queries == 1
    assert quality.avg_top_similarity == pytest.approx(0.90)
    assert quality.weak_matches == 0


@pytest.mark.asyncio
async def test_endpoint_reports_the_threshold_it_used(client, auth_headers):
    """The threshold travels with the numbers, so a reader can interpret them."""
    await _log("weak", 0.10)

    resp = await client.get("/api/search/quality", headers=auth_headers)

    assert resp.status_code == 200
    body = resp.json()
    assert body["queries"] == 1
    assert body["weak_matches"] == 1
    assert body["weak_threshold"] == search_service.WEAK_MATCH_SIMILARITY
    assert body["window_hours"] == search_service.SEARCH_QUALITY_WINDOW_HOURS


@pytest.mark.asyncio
async def test_endpoint_requires_a_token(client):
    resp = await client.get("/api/search/quality")

    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_asking_does_not_record_a_search(client, auth_headers):
    """The diagnostic must not feed itself.

    If reading the page logged a query, the number would drift every time
    anyone looked at it.
    """
    await _log("real search", 0.70)

    await client.get("/api/search/quality", headers=auth_headers)
    quality = await search_service.recent_search_quality()

    assert quality.queries == 1
