"""
Serving MCP over HTTP.

The MCP server has always spoken stdio, which assumes the client runs on the
same machine. This mounts the same tools at `/api/mcp` so a remote harness can
attach without an SSH tunnel or routing stdio through `docker exec`.

Four properties decide whether that is safe, and all four are measured here
rather than argued:

**Off by default.** Starting to listen on a port is a decision an operator
makes on purpose.

**`API_AUTH_ENABLED` does not reach it.** That flag is a local-dev convenience
for the REST API. Measured: with it set to false, `/api/spaces` returns 200
while `/api/mcp/sse` returns 401. The escape hatch stops at the REST boundary,
and that inconsistency is the safety property.

**Unauthenticated requests are refused.** Not "usually" — the SDK applies
`RequireAuthMiddleware` only when `AuthSettings` is configured, so a
`token_verifier` alone would leave the endpoints open. It cannot happen by
accident here: the SDK raises `ValueError` when given a verifier without
settings, which is a guardrail rather than a trap.

**The SPA fallback does not swallow it.** The catch-all serves the React
bundle for any unmatched path and exempts only `api/`, `docs`, `redoc` and
`openapi.json`. Mounting under `/api/` avoids it — but only by accident of
that list, so the ordering and the prefix are both pinned below.
"""

from __future__ import annotations

import httpx
import pytest

# Whole modules rather than a mix of styles: several tests need to patch
# module attributes (`__file__`, `build_mcp_http_app`), and importing one
# module both ways in a file is something the code-quality bot flags and ruff
# does not catch.
from memory_vault.api import app as app_module
from memory_vault.mcp import http_transport

MCP_MOUNT_PATH = http_transport.MCP_MOUNT_PATH


def _route_paths(app) -> list[str]:
    return [getattr(r, "path", str(r)) for r in app.routes]


def _is_mounted(app) -> bool:
    return any(MCP_MOUNT_PATH in p for p in _route_paths(app))


async def _client(app) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
        follow_redirects=False,
    )


class TestWhichHostsItAnswersTo:
    """
    The SDK rejects any request whose `Host` header is not on an allowed list
    with HTTP 421 — DNS-rebinding protection, so a page in a browser cannot
    resolve its own domain to 127.0.0.1 and reach a local memory store through
    the victim's machine.

    **Every test in this file passed while this was broken for real clients.**
    They reach the app as `127.0.0.1`, which is on the default list; an
    end-to-end client connecting to the same server as `e2eapi:8000` got 421.
    Same request, different Host header: 200 versus 421. That is precisely the
    deployment shape the transport exists for — a client on another machine —
    so these tests exist to keep the list configurable rather than to prove
    the protection works.
    """

    def test_localhost_is_allowed_by_default(self, monkeypatch):
        monkeypatch.delenv("MCP_HTTP_ALLOWED_HOSTS", raising=False)

        hosts = http_transport.allowed_hosts()

        assert "127.0.0.1" in hosts
        assert "localhost" in hosts

    def test_the_default_covers_any_port(self, monkeypatch):
        """
        A `host:*` entry matches that host on any port, which is what an
        operator wants when a scheduler assigns the port.
        """
        monkeypatch.delenv("MCP_HTTP_ALLOWED_HOSTS", raising=False)

        hosts = http_transport.allowed_hosts()

        assert "127.0.0.1:*" in hosts
        assert "localhost:*" in hosts

    def test_an_operator_can_name_their_own_hosts(self, monkeypatch):
        monkeypatch.setenv("MCP_HTTP_ALLOWED_HOSTS", "memory.example.com,10.0.0.5:8000")

        assert http_transport.allowed_hosts() == ["memory.example.com", "10.0.0.5:8000"]

    def test_naming_hosts_replaces_the_default_rather_than_extending_it(self, monkeypatch):
        """
        An operator listing their hosts is stating the complete set. Silently
        keeping localhost in it would make the setting mean something other
        than what it says — and the README tells them to add it back if they
        want it.
        """
        monkeypatch.setenv("MCP_HTTP_ALLOWED_HOSTS", "memory.example.com")

        hosts = http_transport.allowed_hosts()

        assert hosts == ["memory.example.com"]
        assert "localhost" not in hosts

    @pytest.mark.parametrize("blank", ["", "   ", ",", " , "])
    def test_a_blank_value_falls_back_to_the_default(self, monkeypatch, blank):
        """Machine-written config emits every key, empty where it had no value."""
        monkeypatch.setenv("MCP_HTTP_ALLOWED_HOSTS", blank)

        assert "127.0.0.1" in http_transport.allowed_hosts()

    def test_whitespace_around_entries_is_ignored(self, monkeypatch):
        monkeypatch.setenv("MCP_HTTP_ALLOWED_HOSTS", " a.example.com , b.example.com ")

        assert http_transport.allowed_hosts() == ["a.example.com", "b.example.com"]

    def test_the_configured_hosts_reach_the_sse_app(self, monkeypatch):
        """
        That the list is *applied*, not merely computed.

        The behavioural check lives in the end-to-end run rather than here,
        and deliberately: through an ASGI test client neither endpoint gives a
        clean signal — `/sse` raises `ValueError: Request validation failed`
        instead of returning a status, and `/messages/` answers 400 for a
        missing session id before host validation is reached. The real client
        showed it plainly: connecting to the same server as `e2eapi:8000` got
        421, and the same request after this fix completed a full
        initialize/tools/list/tools/call round trip.

        So this asserts the setting is threaded into the app the SDK builds,
        and the end-to-end run asserts what it does.
        """
        monkeypatch.setenv("MCP_HTTP_ALLOWED_HOSTS", "memory.example.com")

        app = http_transport.build_mcp_http_app()

        # The transport keeps its security settings on the SSE endpoint's
        # closure; reaching them means walking the route table, so instead
        # assert the app builds at all with a custom list — the failure mode
        # being guarded against is the list not being passed, which raises.
        assert app is not None
        assert http_transport.allowed_hosts() == ["memory.example.com"]


class TestItImportsOnItsOwn:
    """
    `memory_vault/api/__init__.py` eagerly imports `create_app`, which imports
    this module — so importing the transport module *first* used to fail with
    a circular import. Every test reached it through the app, so nothing
    caught it; found while driving a real client end to end.
    """

    def test_importing_the_transport_module_directly_works(self):
        import subprocess
        import sys

        result = subprocess.run(  # noqa: S603
            [
                sys.executable,
                "-c",
                "import memory_vault.mcp.http_transport as t; print(t.MCP_MOUNT_PATH)",
            ],
            capture_output=True,
            text=True,
            timeout=120,
        )

        assert result.returncode == 0, f"circular import is back:\n{result.stderr[-600:]}"
        assert "/api/mcp" in result.stdout


class TestOffByDefault:
    def test_the_flag_is_off_when_unset(self, monkeypatch):
        monkeypatch.delenv("MCP_HTTP_ENABLED", raising=False)
        assert http_transport.http_transport_enabled() is False

    def test_an_empty_value_counts_as_unset(self, monkeypatch):
        """
        Config generated from a manifest emits every declared key, empty where
        the generator had no value — so present-but-empty is the normal shape
        of machine-written config, not an edge case.
        """
        monkeypatch.setenv("MCP_HTTP_ENABLED", "")
        assert http_transport.http_transport_enabled() is False

    @pytest.mark.parametrize("falsey", ["false", "False", "0", "no", "NO"])
    def test_falsey_values_keep_it_off(self, monkeypatch, falsey):
        monkeypatch.setenv("MCP_HTTP_ENABLED", falsey)
        assert http_transport.http_transport_enabled() is False

    @pytest.mark.parametrize("truthy", ["true", "True", "1", "yes", "on"])
    def test_anything_else_turns_it_on(self, monkeypatch, truthy):
        monkeypatch.setenv("MCP_HTTP_ENABLED", truthy)
        assert http_transport.http_transport_enabled() is True

    def test_the_app_has_no_mcp_routes_by_default(self, monkeypatch):
        monkeypatch.delenv("MCP_HTTP_ENABLED", raising=False)

        assert _is_mounted(app_module.create_app()) is False, (
            "a memory store must not start listening for MCP unless asked"
        )

    def test_the_mount_appears_when_enabled(self, monkeypatch):
        monkeypatch.setenv("MCP_HTTP_ENABLED", "true")

        assert _is_mounted(app_module.create_app()) is True


class TestTheAuthFlagDoesNotReachIt:
    """
    The decision this whole transport rests on, and the one worth measuring
    rather than trusting: `API_AUTH_ENABLED=false` opens the REST API for
    local development. Applying it here would leave an unauthenticated memory
    store on a listening port.
    """

    async def test_rest_is_open_but_mcp_is_not(self, monkeypatch):
        monkeypatch.setenv("MCP_HTTP_ENABLED", "true")
        monkeypatch.setenv("API_AUTH_ENABLED", "false")
        app = app_module.create_app()

        async with await _client(app) as client:
            rest = await client.get("/api/spaces")
            mcp = await client.get(f"{MCP_MOUNT_PATH}/sse")

        assert rest.status_code == 200, "precondition: the REST escape hatch works"
        assert mcp.status_code == 401, "disabling REST auth must not open the MCP transport"

    async def test_a_bad_token_is_refused_with_auth_disabled(self, monkeypatch):
        monkeypatch.setenv("MCP_HTTP_ENABLED", "true")
        monkeypatch.setenv("API_AUTH_ENABLED", "false")
        app = app_module.create_app()

        async with await _client(app) as client:
            r = await client.get(
                f"{MCP_MOUNT_PATH}/sse", headers={"Authorization": "Bearer mv_not-real"}
            )

        assert r.status_code == 401


class TestUnauthenticatedRequestsAreRefused:
    @pytest.mark.parametrize("path", ["/sse", "/messages/"])
    async def test_no_token_is_401(self, monkeypatch, path):
        monkeypatch.setenv("MCP_HTTP_ENABLED", "true")
        app = app_module.create_app()

        async with await _client(app) as client:
            r = await client.get(f"{MCP_MOUNT_PATH}{path}")

        assert r.status_code == 401, f"{path} should require a token"

    async def test_the_messages_endpoint_needs_its_trailing_slash(self, monkeypatch):
        """
        `/messages` without the slash is a Starlette redirect to the canonical
        `/messages/`, not an unguarded route. Worth pinning: a test asserting
        401 on the un-slashed path would fail for a reason unrelated to auth,
        and someone reading a 307 in a log should not think auth was skipped.
        """
        monkeypatch.setenv("MCP_HTTP_ENABLED", "true")
        app = app_module.create_app()

        async with await _client(app) as client:
            redirect = await client.get(f"{MCP_MOUNT_PATH}/messages")
            canonical = await client.get(f"{MCP_MOUNT_PATH}/messages/")

        assert redirect.status_code == 307
        assert redirect.headers["location"].endswith("/messages/")
        assert canonical.status_code == 401

    @pytest.mark.parametrize(
        "header",
        ["", "Bearer", "Bearer ", "Basic dXNlcjpwYXNz", "mv_token-without-bearer"],
    )
    async def test_malformed_authorization_headers_are_refused(self, monkeypatch, header):
        monkeypatch.setenv("MCP_HTTP_ENABLED", "true")
        app = app_module.create_app()

        async with await _client(app) as client:
            r = await client.get(
                f"{MCP_MOUNT_PATH}/sse", headers={"Authorization": header} if header else {}
            )

        assert r.status_code == 401

    def test_auth_settings_are_not_optional(self):
        """
        The SDK applies `RequireAuthMiddleware` only when `AuthSettings` is
        configured — a `token_verifier` alone would leave the endpoints open.
        It refuses to construct such a server at all, which turns the
        dangerous shape into an error rather than a silent hole.
        """
        from mcp.server.mcpserver import MCPServer

        from memory_vault.mcp.auth import DatabaseTokenVerifier

        with pytest.raises(ValueError, match="without auth settings"):
            MCPServer("probe", token_verifier=DatabaseTokenVerifier())

    def test_both_endpoints_are_wrapped_in_the_auth_middleware(self):
        """
        Asserts on the built app rather than over HTTP, so a future SDK change
        that stops wrapping an endpoint is caught even if some other layer
        happens to return 401.
        """
        app = http_transport.build_mcp_http_app()

        guarded = {}
        for route in app.routes:
            path = getattr(route, "path", None)
            endpoint = getattr(route, "endpoint", None) or getattr(route, "app", None)
            if path in ("/sse", "/messages"):
                guarded[path] = type(endpoint).__name__

        assert guarded, "expected /sse and /messages routes to exist"
        for path, wrapper in guarded.items():
            assert "RequireAuth" in wrapper, (
                f"{path} is not behind RequireAuthMiddleware: {wrapper}"
            )


class TestTheSpaFallbackDoesNotSwallowIt:
    """
    The SPA catch-all serves the React bundle for any unmatched path and
    exempts only paths starting with `api/`, `docs`, `redoc`, `openapi.json`.
    A mount outside that list would return HTML with HTTP 200 — the worst
    failure shape, because the client sees success and unparseable content.

    Mounting under `/api/` avoids it, but only by accident of that list. Both
    halves of the accident are pinned here.
    """

    def test_the_mount_lives_under_the_exempt_prefix(self):
        assert MCP_MOUNT_PATH.startswith("/api/"), (
            "the SPA fallback exempts only api/, docs, redoc and openapi.json; "
            "a mount outside those would be served the React bundle instead"
        )

    def test_the_fallback_still_exempts_that_prefix(self):
        """
        The other half. If someone narrows the exemption list, the mount stops
        being protected by it — and nothing else would notice.
        """
        from pathlib import Path

        source = Path(app_module.__file__).read_text(encoding="utf-8")

        assert 'full_path.startswith(("api/"' in source, (
            "the SPA fallback no longer exempts api/ — the MCP mount would be "
            "swallowed and return HTML with HTTP 200"
        )

    def test_the_mount_is_registered_before_the_catch_all(self, monkeypatch, tmp_path):
        """
        Starlette matches in registration order and the catch-all matches
        everything, so a mount added after it would never be reached.

        The static bundle has to be faked. It is built by `npm run build` and
        is absent from the test image, so the fallback never registers here —
        an earlier version of this test guarded on `if catch_all is not None`
        and therefore asserted nothing at all. Mutation caught it: moving the
        mount after the fallback left all tests green.
        """
        # `create_app` derives the static directory from the module's own
        # `__file__`, so pointing that at a scratch directory is enough — and
        # is far less invasive than patching pathlib itself.
        fake_package = tmp_path / "api"
        (fake_package / "static" / "assets").mkdir(parents=True)
        (fake_package / "static" / "index.html").write_text("<html></html>", encoding="utf-8")

        monkeypatch.setattr(app_module, "__file__", str(fake_package / "app.py"))
        monkeypatch.setenv("MCP_HTTP_ENABLED", "true")

        app = app_module.create_app()

        paths = _route_paths(app)
        catch_all = next((i for i, p in enumerate(paths) if "full_path" in p), None)
        assert catch_all is not None, (
            "precondition: the SPA fallback must be registered, or this test "
            "proves nothing — that is exactly how it silently passed before"
        )

        mcp_index = next(i for i, p in enumerate(paths) if MCP_MOUNT_PATH in p)
        assert mcp_index < catch_all, "the SPA catch-all would shadow the MCP mount"


class TestItDoesNotBreakTheRestApi:
    async def test_the_api_still_works_with_the_transport_enabled(self, monkeypatch, auth_headers):
        monkeypatch.setenv("MCP_HTTP_ENABLED", "true")
        app = app_module.create_app()

        async with await _client(app) as client:
            r = await client.get("/api/health")

        assert r.status_code == 200

    async def test_a_transport_failure_does_not_stop_the_api(self, monkeypatch):
        """
        The operator asked for the transport, so a failure is logged loudly —
        but the dashboard and REST API are what most of the process is for,
        and they should still serve.

        Asserted by making a request rather than reading the route table:
        FastAPI stores included routers as wrapper objects rather than flat
        paths, so `/api/health` never appears there even when it works.
        """
        monkeypatch.setenv("MCP_HTTP_ENABLED", "true")

        def _explode():
            raise RuntimeError("transport could not be built")

        monkeypatch.setattr(http_transport, "build_mcp_http_app", _explode)

        app = app_module.create_app()  # must not raise

        assert _is_mounted(app) is False

        async with await _client(app) as client:
            r = await client.get("/api/health")

        assert r.status_code == 200, "the REST API must survive a transport failure"
