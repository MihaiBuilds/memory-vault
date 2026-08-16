"""
CI smoke test — proves memory_vault.mcp.server imports cleanly.

Historically no test in this suite imported the MCP server module, so a
transitive dep drift (mcp 2.0.0 removing mcp.server.fastmcp on 2026-07-28)
silently shipped as v1.0.9 and left the MCP-only docker image dead on
arrival for six days before anyone noticed. See issue #126.

This test is intentionally minimal: it exists so any regression that breaks
the MCP server's import fails CI immediately, whether it comes from an
upstream mcp release, a local edit, or a pyproject drift. Any test that
imports memory_vault.mcp.server would serve this purpose; a dedicated
smoke test makes the intent explicit and keeps the failure message
obvious ("MCP server module failed to import") when it triggers.
"""

from __future__ import annotations

import pytest

TOOL_NAMES = ("recall", "remember", "forget", "memory_status", "move_memory")


def test_mcp_server_module_imports_cleanly():
    """Import the MCP server module. Any ImportError here blocks the release."""
    import memory_vault.mcp.server as mcp_server

    # Confirm the server instance was constructed at module load. If the SDK
    # moved the class again the import above already raised — this line is
    # defensive against a future refactor that makes it a lazy factory.
    assert mcp_server.mcp is not None


def test_mcp_server_tools_are_exported():
    """The canonical tools must be present on the server module."""
    import memory_vault.mcp.server as mcp_server

    for tool_name in TOOL_NAMES:
        assert hasattr(mcp_server, tool_name), (
            f"MCP tool `{tool_name}` is not exported from memory_vault.mcp.server"
        )


@pytest.mark.asyncio
async def test_mcp_tools_are_registered_with_the_server():
    """The tools are registered on the server, not merely defined in the module.

    Checking module attributes alone would still pass if the decorator stopped
    registering: the functions would exist and the server would advertise
    nothing. Asking the server what it exposes is what a client actually sees.
    """
    import memory_vault.mcp.server as mcp_server

    tools = await mcp_server.mcp.list_tools()
    registered = {t.name for t in tools}

    missing = set(TOOL_NAMES) - registered
    assert not missing, f"tools defined but not registered with the server: {missing}"


@pytest.mark.asyncio
async def test_registered_tools_advertise_their_arguments():
    """Each tool exposes a schema and a description, so a client can use it.

    A tool that registers with an empty schema imports and lists cleanly while
    being uncallable from the client side.
    """
    import memory_vault.mcp.server as mcp_server

    tools = {t.name: t for t in await mcp_server.mcp.list_tools()}

    assert "text" in tools["remember"].input_schema.get("properties", {})
    assert "query" in tools["recall"].input_schema.get("properties", {})
    assert "chunk_id" in tools["forget"].input_schema.get("properties", {})
    for name in ("chunk_id", "target_space"):
        assert name in tools["move_memory"].input_schema.get("properties", {})

    # The docstring is what a client shows a user; an empty description means
    # the tool arrived without its explanation.
    for name in TOOL_NAMES:
        assert (tools[name].description or "").strip(), f"{name} has no description"
