"""Serving MCP over HTTP, mounted into the REST application.

The MCP server has always spoken stdio, which assumes the client runs on the
same machine. Remote harnesses cannot attach without an SSH tunnel or routing
stdio through `docker exec`. This mounts the same tools at `/api/mcp` so a
client elsewhere on the network can reach them.

Off unless `MCP_HTTP_ENABLED` says otherwise. A memory store is not something
to start listening on a port by default.
"""

from __future__ import annotations

import logging
import os

from starlette.applications import Starlette

from memory_vault.config import env_str
from memory_vault.mcp.auth import MCP_SCOPE, DatabaseTokenVerifier

logger = logging.getLogger(__name__)

# Mounted under `/api/` deliberately. The SPA fallback serves the React bundle
# for any unmatched path and exempts only paths starting with `api/`, `docs`,
# `redoc` and `openapi.json` — so a mount anywhere else would return the
# dashboard with HTTP 200 instead of failing loudly, and a client would see
# HTML where it expected an event stream. `test_mcp_http_mount.py` pins this.
MCP_MOUNT_PATH = "/api/mcp"

# Where the transport tells clients it lives. Only used to populate the OAuth
# resource-metadata document the SDK serves; MV issues tokens with
# `memory-vault token create` rather than through an OAuth flow, so no client
# ever visits the issuer. It has to be a well-formed URL, not a real endpoint.
_DEFAULT_PUBLIC_URL = "http://127.0.0.1:8000"


def http_transport_enabled() -> bool:
    """Whether to mount MCP over HTTP.

    Off unless explicitly turned on, and deliberately not tied to
    `API_AUTH_ENABLED` or any other flag: starting to listen on a port is a
    decision an operator makes on purpose.

    Reads the same way `auth_enabled` does — anything but a recognised falsey
    string counts as on, once the flag is present at all.
    """
    raw = os.getenv("MCP_HTTP_ENABLED")
    if raw is None or not raw.strip():
        return False
    return raw.strip().lower() not in ("false", "0", "no")


def build_mcp_http_app() -> Starlette:
    """Build the SSE transport app, with authentication wired in.

    SSE rather than `streamable-http`: the transport standard clients document
    and support today. Both are available in the SDK and both return mountable
    apps, so adding the newer one later is a second mount rather than a
    migration.

    `AuthSettings` is not optional here, and the SDK enforces that — passing a
    `token_verifier` without it raises `ValueError`. That is worth knowing
    because the alternative would have been the dangerous shape: a verifier
    that looks configured while `RequireAuthMiddleware` is never applied,
    leaving the endpoints open. With both set, `/sse` and `/messages` are each
    wrapped in `RequireAuthMiddleware`.
    """
    # Imported here rather than at module scope: importing the MCP server
    # module builds the tool registry and pulls in the embedding stack, which
    # is wasted work for the common case where the transport is disabled.
    from mcp.server.auth.settings import AuthSettings
    from mcp.server.mcpserver import MCPServer

    from memory_vault.mcp.server import (
        forget,
        memory_status,
        move_memory,
        purge_forgotten,
        recall,
        remember,
    )
    from memory_vault.mcp.server import mcp as stdio_server

    public_url = env_str("MCP_HTTP_PUBLIC_URL", _DEFAULT_PUBLIC_URL)

    # A second MCPServer over the same tools. Auth settings are fixed at
    # construction and the stdio instance is built at import without them, so
    # the HTTP surface needs its own instance rather than mutating the one
    # every existing stdio client depends on.
    http_server = MCPServer(
        stdio_server.name,
        version=stdio_server.version,
        token_verifier=DatabaseTokenVerifier(),
        auth=AuthSettings(
            issuer_url=public_url,
            resource_server_url=public_url,
            required_scopes=[MCP_SCOPE],
            # Audience checking off, explicitly rather than by omission. It
            # compares a token's audience against `resource_server_url`, which
            # only means something for tokens minted by an OAuth issuer for a
            # named resource. MV's are opaque strings from `token create` with
            # no audience to check, so leaving this on would refuse every real
            # token. The SDK warns when it is unset because the default flips
            # in 3.0 — stating it keeps that upgrade from silently locking
            # everyone out.
            validate_token_resource=False,
        ),
    )

    # Re-registered through the public `add_tool` rather than by copying the
    # server's private tool registry: the decorator on each function registers
    # it with the stdio instance, but the functions themselves are ordinary
    # importable callables. Reaching into a private attribute would work today
    # and break silently the first time the SDK renames it.
    for tool in (recall, remember, forget, purge_forgotten, move_memory, memory_status):
        http_server.add_tool(tool)

    return http_server.sse_app()


def mount_mcp_http(app) -> bool:
    """Mount the MCP transport onto `app` if it is enabled.

    Returns whether it was mounted, so a caller can log or test the decision
    rather than inferring it from the route table.

    Must be called before the SPA catch-all is registered. Starlette matches
    routes in registration order, and the catch-all matches everything.
    """
    if not http_transport_enabled():
        return False

    try:
        app.mount(MCP_MOUNT_PATH, build_mcp_http_app())
    except Exception:
        # A transport that fails to build must not stop the REST API from
        # serving. The operator asked for it, so this is loud — but the
        # process still starts, and the dashboard and API still work.
        logger.exception(
            "MCP HTTP transport failed to start; the REST API is unaffected. "
            "MCP over HTTP will not be available at %s",
            MCP_MOUNT_PATH,
        )
        return False

    logger.info(
        "MCP HTTP transport mounted at %s (SSE). Requests require a bearer "
        "token regardless of API_AUTH_ENABLED.",
        MCP_MOUNT_PATH,
    )
    return True
