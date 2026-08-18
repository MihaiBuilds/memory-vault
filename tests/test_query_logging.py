"""
Query logging — MCP recall writes to query_log.

memory_status reports queries_24h from query_log; recall must feed it
(the observability counter previously always read 0 for MCP usage).
"""

from __future__ import annotations

import json

import pytest

from memory_vault.mcp import server as mcp_server


@pytest.mark.asyncio
async def test_recall_writes_query_log():
    await mcp_server.remember(text="Query logging smoke fact about llamas.")

    res = json.loads(await mcp_server.recall(query="tell me about llamas"))
    assert res["status"] == "ok"

    from memory_vault.models.db import fetch_one

    row = await fetch_one(
        "SELECT query_text, result_count, latency_ms FROM query_log "
        "ORDER BY created_at DESC LIMIT 1"
    )
    assert row is not None
    assert row["query_text"] == "tell me about llamas"
    assert row["result_count"] >= 1
    assert row["latency_ms"] is not None
