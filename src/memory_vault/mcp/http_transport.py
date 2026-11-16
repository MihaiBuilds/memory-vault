"""Serving MCP over HTTP, mounted into the REST application.

The MCP server has always spoken stdio, which assumes the client runs on the
same machine. Remote harnesses cannot attach without an SSH tunnel or routing
stdio through `docker exec`. This mounts the same tools at `/api/mcp` so a
client elsewhere on the network can reach them.

Two transports share that mount, because clients split between them: SSE at
`/api/mcp/sse`, and Streamable HTTP — the newer one in the MCP spec — at
`/api/mcp/http`. A client that speaks only one of them cannot fall back to the
other.

Off unless `MCP_HTTP_ENABLED` says otherwise. A memory store is not something
to start listening on a port by default.
"""

from __future__ import annotations

import logging
import os
from contextlib import asynccontextmanager

from starlette.applications import Starlette

from memory_vault.config import env_str

# `memory_vault.mcp.auth` is imported inside `build_mcp_http_app`, not here.
# It reads the token lookup from `memory_vault.api.deps`, and
# `memory_vault/api/__init__.py` eagerly imports `create_app`, which imports
# this module — so importing it at module scope makes
# `import memory_vault.mcp.http_transport` fail with a circular import. It
# only shows up when this module is imported *first*; every test reached it
# through the app, which is why nothing caught it.

logger = logging.getLogger(__name__)

# Mounted under `/api/` deliberately. The SPA fallback serves the React bundle
# for any unmatched path and exempts only paths starting with `api/`, `docs`,
# `redoc` and `openapi.json` — so a mount anywhere else would return the
# dashboard with HTTP 200 instead of failing loudly, and a client would see
# HTML where it expected an event stream. `test_mcp_http_mount.py` pins this.
MCP_MOUNT_PATH = "/api/mcp"

# Where Streamable HTTP lives inside that mount, so clients use
# `/api/mcp/http`. Everything else under the mount stays with SSE, which keeps
# `/sse` and `/messages/` exactly where existing clients expect them.
STREAMABLE_HTTP_PATH = "/http"

# Where the transport tells clients it lives. Only used to populate the OAuth
# resource-metadata document the SDK serves; MV issues tokens with
# `memory-vault token create` rather than through an OAuth flow, so no client
# ever visits the issuer. It has to be a well-formed URL, not a real endpoint.
_DEFAULT_PUBLIC_URL = "http://127.0.0.1:8000"

# Hostnames the transport will answer to. The SDK rejects any request whose
# Host header is not on this list with HTTP 421, which is DNS-rebinding
# protection: without it, a page in a browser could resolve its own domain to
# 127.0.0.1 and reach a local memory store through the victim's machine.
#
# The default covers a client on the same machine. Anything else — a container
# name, a LAN address, a hostname behind a reverse proxy — has to be declared,
# because the whole point of the check is that the server knows which names
# are legitimately its own. Found the hard way: an end-to-end client reaching
# the server as `e2eapi:8000` got 421 while every test passed, because the
# tests reached it as 127.0.0.1.
_DEFAULT_ALLOWED_HOSTS = ("127.0.0.1", "127.0.0.1:*", "localhost", "localhost:*")


def allowed_hosts() -> list[str]:
    """Hostnames the MCP transport will answer to.

    `MCP_HTTP_ALLOWED_HOSTS` is a comma-separated list, and it *replaces* the
    localhost default rather than extending it — an operator naming their
    hosts is stating the complete set, and silently keeping localhost in it
    would make the setting mean something other than what it says. Add
    `localhost` explicitly to keep it.

    A `host:*` entry matches that host on any port, which the SDK supports and
    is usually what an operator wants when the port is assigned by a
    scheduler.
    """
    raw = os.getenv("MCP_HTTP_ALLOWED_HOSTS")
    if raw is None or not raw.strip():
        return list(_DEFAULT_ALLOWED_HOSTS)

    hosts = [h.strip() for h in raw.split(",") if h.strip()]
    return hosts or list(_DEFAULT_ALLOWED_HOSTS)


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


def _route_path(scope) -> str:
    """The request path relative to the mount, the way Starlette's router sees it.

    A mounted app is handed the full path, with the mount prefix carried in
    `root_path`, so the prefix has to come off before comparing.
    """
    path = scope.get("path", "")
    root = scope.get("root_path", "")
    if root and path.startswith(root):
        path = path[len(root) :]
    return path or "/"


class TransportRouter:
    """Send the Streamable HTTP path to that transport and everything else to SSE.

    Both transports sit under one mount. Two separate mounts would also work,
    but only in one registration order: with `/api/mcp` registered first, a
    mount nested at `/api/mcp/http` is never reached — the outer mount claims
    every path beneath it and its SSE app answers 404. That was measured
    against the SDK rather than assumed. Routing by exact path inside a single
    mount takes registration order out of the picture.

    Each transport stays the app the SDK built, with its own auth middleware.
    Merging their route lists into one app would look tidier and quietly drop
    that middleware.
    """

    def __init__(self, sse_app: Starlette, streamable_app: Starlette, session_manager) -> None:
        self.sse_app = sse_app
        self.streamable_app = streamable_app
        self.session_manager = session_manager

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] == "http" and _route_path(scope) == STREAMABLE_HTTP_PATH:
            await self.streamable_app(scope, receive, send)
            return
        await self.sse_app(scope, receive, send)


def build_mcp_http_app() -> TransportRouter:
    """Build both transport apps, with authentication wired in.

    SSE came first because it was the transport clients documented and
    supported when MCP over HTTP was added. Clients have since appeared that
    speak only Streamable HTTP: they `POST` straight to the URL they were
    given, which the SSE endpoint answers with 405. Both transports come from
    the same server instance, so they share one set of tools, one token
    verifier and one Host allow-list.

    `AuthSettings` is not optional here, and the SDK enforces that — passing a
    `token_verifier` without it raises `ValueError`. That is worth knowing
    because the alternative would have been the dangerous shape: a verifier
    that looks configured while `RequireAuthMiddleware` is never applied,
    leaving the endpoints open. With both set, `/sse`, `/messages` and `/http`
    are each wrapped in `RequireAuthMiddleware`.
    """
    # Imported here rather than at module scope: importing the MCP server
    # module builds the tool registry and pulls in the embedding stack, which
    # is wasted work for the common case where the transport is disabled.
    from mcp.server.auth.settings import AuthSettings
    from mcp.server.mcpserver import MCPServer
    from mcp.server.transport_security import TransportSecuritySettings

    from memory_vault.mcp.auth import MCP_SCOPE, DatabaseTokenVerifier
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

    # DNS-rebinding protection stays on; the operator says which names are
    # theirs. Origins mirror the host list so a browser-based client can reach
    # the transport from the same names it connects to.
    hosts = allowed_hosts()
    security = TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=hosts,
        allowed_origins=[f"http://{h}" for h in hosts] + [f"https://{h}" for h in hosts],
    )

    sse_app = http_server.sse_app(transport_security=security)

    # The router hands requests over with the scope unchanged, so the SDK has
    # to be looking for the same path the router matched on. Left at its
    # default of `/mcp`, every request the router sent here would 404.
    streamable_app = http_server.streamable_http_app(
        streamable_http_path=STREAMABLE_HTTP_PATH,
        transport_security=security,
    )

    # Read only after `streamable_http_app()`: the SDK creates the session
    # manager lazily and raises `RuntimeError` if it is asked for first.
    return TransportRouter(sse_app, streamable_app, http_server.session_manager)


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
        transport = build_mcp_http_app()
        app.mount(MCP_MOUNT_PATH, transport)
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

    # Stored only once the mount has succeeded, and that ordering is the
    # point: `mcp_http_lifespan` runs whatever is stored here. Had it been
    # keyed to `MCP_HTTP_ENABLED` instead, a failed build above would leave it
    # asking the SDK for a session manager that was never created — which
    # raises at startup and takes the REST API down with it, undoing the
    # guarantee the `except` above exists to make.
    app.state.mcp_session_manager = transport.session_manager

    logger.info(
        "MCP HTTP transport mounted: Streamable HTTP at %s%s, SSE at %s/sse. "
        "Requests require a bearer token regardless of API_AUTH_ENABLED.",
        MCP_MOUNT_PATH,
        STREAMABLE_HTTP_PATH,
        MCP_MOUNT_PATH,
    )
    return True


@asynccontextmanager
async def mcp_http_lifespan(app):
    """Run the Streamable HTTP session manager for as long as the app runs.

    SSE needs nothing at startup. Streamable HTTP does: its session manager
    owns the task group every request runs in, and until `run()` has been
    entered each request fails with `RuntimeError: Task group is not
    initialized` — a 500. A mounted app's own lifespan is never run by its
    parent, so the parent has to do it.

    Does nothing when no manager was stored: the transport is off, or it
    failed to build. Kept separate from the app's lifespan so it can be
    entered without also opening and closing the database pool.
    """
    manager = getattr(app.state, "mcp_session_manager", None)
    if manager is None:
        yield
        return

    async with manager.run():
        yield
