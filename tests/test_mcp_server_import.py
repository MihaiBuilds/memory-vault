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


def test_mcp_server_module_imports_cleanly():
    """Import the MCP server module. Any ImportError here blocks the release."""
    import memory_vault.mcp.server as mcp_server

    # Confirm the FastMCP instance was constructed at module load. If mcp.server.fastmcp
    # is missing (mcp 2.0.0+) the import above already raised — this line is defensive
    # against a future refactor that turns FastMCP into a lazy factory.
    assert mcp_server.mcp is not None


def test_mcp_server_tools_are_registered():
    """The four canonical tools must be present on the server module."""
    import memory_vault.mcp.server as mcp_server

    for tool_name in ("recall", "remember", "forget", "memory_status"):
        assert hasattr(mcp_server, tool_name), (
            f"MCP tool `{tool_name}` is not exported from memory_vault.mcp.server"
        )
