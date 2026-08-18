"""
Ranking: retrieval rank must dominate importance/recency boosts.

The importance (max +0.15) and recency (max +0.05) boosts were additive
on top of RRF scores whose rank-1 contribution is only ~0.016, so any
recent high-importance chunk outscored an exact match with default
importance — burying it beyond the top results. Boosts must scale the
RRF score, not swamp it: a chunk that is rank 1 in BOTH arms cannot be
pushed out of the top results by boosts alone.
"""

from __future__ import annotations

import json

import pytest

from memory_vault.mcp import server as mcp_server


@pytest.mark.asyncio
async def test_dual_rank1_match_beats_recent_high_importance_chunks():
    # A field of fresh, high-importance chunks (classified as decisions),
    # semantically unrelated to the probe query.
    for i in range(12):
        await mcp_server.remember(
            text=f"We decided to adopt build pipeline number {i} for the web deployment."
        )

    # The probe: default importance ("fact"), exact match for the query.
    probe = json.loads(
        await mcp_server.remember(text="The vault's mascot is a stuffed capybara named Ada.")
    )

    res = json.loads(await mcp_server.recall(query="vault mascot capybara", limit=3))
    top_ids = [r["chunk_id"] for r in res["results"]]
    assert probe["chunk_id"] in top_ids, (
        "exact match (rank 1 in both arms) must not be buried by importance/recency boosts"
    )
