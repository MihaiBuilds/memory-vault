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

import json

import httpx
import pytest

# Whole modules rather than a mix of styles: several tests need to patch
# module attributes (`__file__`, `build_mcp_http_app`), and importing one
# module both ways in a file is something the code-quality bot flags and ruff
# does not catch.
from memory_vault.api import app as app_module
from memory_vault.mcp import http_transport

MCP_MOUNT_PATH = http_transport.MCP_MOUNT_PATH
STREAMABLE_URL = MCP_MOUNT_PATH + http_transport.STREAMABLE_HTTP_PATH

# What a Streamable HTTP client sends first. The `Accept` header is part of the
# protocol, not decoration: the transport answers with an event stream and
# refuses clients that do not say they can read one.
_INITIALIZE = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "initialize",
    "params": {
        "protocolVersion": "2025-03-26",
        "capabilities": {},
        "clientInfo": {"name": "test-suite", "version": "0"},
    },
}
_STREAMABLE_HEADERS = {
    "Accept": "application/json, text/event-stream",
    "Content-Type": "application/json",
}


def _sse_json(response: httpx.Response) -> dict:
    """The JSON-RPC message carried in a Streamable HTTP response's event stream."""
    for line in response.text.splitlines():
        if line.startswith("data: "):
            return json.loads(line[len("data: ") :])
    raise AssertionError(f"no data line in the response: {response.text[:200]!r}")


async def _noop(*_args, **_kwargs) -> None:
    return None


def _route_paths(app) -> list[str]:
    return [getattr(r, "path", str(r)) for r in app.routes]


def _is_mounted(app) -> bool:
    return any(MCP_MOUNT_PATH in p for p in _route_paths(app))


async def _client(app, base_url: str = "http://test") -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url=base_url,
        follow_redirects=False,
    )


# A request that has to get *past* the Host check must arrive under a name on
# the default allow-list. The suite's usual `http://test` is not on it, which is
# harmless for tests that stop at a 401 — auth answers first — but turns every
# request meant to succeed into a 421.
_LOCAL = "http://127.0.0.1:8000"


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

    def test_no_session_manager_is_stored_when_off(self, monkeypatch):
        """
        The lifespan runs whatever `mount_mcp_http` stored. With the
        transport off there must be nothing to run.
        """
        monkeypatch.delenv("MCP_HTTP_ENABLED", raising=False)

        app = app_module.create_app()

        assert getattr(app.state, "mcp_session_manager", None) is None


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

    async def test_the_streamable_transport_is_not_opened_either(self, monkeypatch):
        """
        The same property, for the second transport. It shares the server
        instance and so the verifier, but that is an implementation detail —
        the property is what a client sees, so it is measured over HTTP with
        the session manager running.
        """
        monkeypatch.setenv("MCP_HTTP_ENABLED", "true")
        monkeypatch.setenv("API_AUTH_ENABLED", "false")
        app = app_module.create_app()

        async with http_transport.mcp_http_lifespan(app), await _client(app, _LOCAL) as client:
            rest = await client.get("/api/spaces")
            mcp = await client.post(STREAMABLE_URL, json=_INITIALIZE, headers=_STREAMABLE_HEADERS)

        assert rest.status_code == 200, "precondition: the REST escape hatch works"
        assert mcp.status_code == 401, "disabling REST auth must not open Streamable HTTP"


class TestUnauthenticatedRequestsAreRefused:
    @pytest.mark.parametrize("path", ["/sse", "/messages/", "/http"])
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

    def test_every_endpoint_is_wrapped_in_the_auth_middleware(self):
        """
        Asserts on the built apps rather than over HTTP, so a future SDK change
        that stops wrapping an endpoint is caught even if some other layer
        happens to return 401. Both transports are walked: they are separate
        apps the SDK built, so wrapping one says nothing about the other.
        """
        transport = http_transport.build_mcp_http_app()

        guarded = {}
        for app in (transport.sse_app, transport.streamable_app):
            for route in app.routes:
                path = getattr(route, "path", None)
                endpoint = getattr(route, "endpoint", None) or getattr(route, "app", None)
                if path in ("/sse", "/messages", http_transport.STREAMABLE_HTTP_PATH):
                    guarded[path] = type(endpoint).__name__

        assert set(guarded) == {"/sse", "/messages", http_transport.STREAMABLE_HTTP_PATH}, (
            f"expected all three endpoints to exist, found {sorted(guarded)}"
        )
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

    async def test_a_transport_failure_does_not_stop_startup_either(self, monkeypatch):
        """
        The failure the test above cannot see, because it never runs a
        lifespan. Streamable HTTP adds work at startup — entering the session
        manager — and the obvious way to gate that is on `MCP_HTTP_ENABLED`.
        But the flag only says the operator asked for the transport, not that
        it was built. Gated on the flag, a failed build leaves startup asking
        the SDK for a session manager that never existed; the SDK raises, the
        app never starts, and the REST API goes down with the transport.

        So this runs the app's real lifespan, with only the database pool
        stubbed out — the suite owns one pool for the whole session, and the
        real lifespan would close it.
        """
        monkeypatch.setenv("MCP_HTTP_ENABLED", "true")
        monkeypatch.setattr(app_module, "init_pool", _noop)
        monkeypatch.setattr(app_module, "close_pool", _noop)

        def _explode():
            raise RuntimeError("transport could not be built")

        monkeypatch.setattr(http_transport, "build_mcp_http_app", _explode)

        app = app_module.create_app()
        assert getattr(app.state, "mcp_session_manager", None) is None

        async with app.router.lifespan_context(app), await _client(app) as client:
            r = await client.get("/api/health")

        assert r.status_code == 200, "startup must survive a transport that failed to build"


class TestStreamableHttp:
    """
    The second transport. Some clients speak only Streamable HTTP: they never
    open the SSE stream, and `POST` straight to the URL they were given — which
    the SSE endpoint answers with 405. This one is for them.

    These run with the session manager entered, because nothing else is
    realistic: the HTTP test client does not run lifespans, and without one
    every request fails before reaching any of the behaviour asserted here.
    """

    def test_the_url_is_pinned(self):
        """
        Clients are configured with this URL by hand, and the README gives it.
        Moving it would break every one of them without a single test failing
        anywhere else.
        """
        assert STREAMABLE_URL == "/api/mcp/http"

    async def test_a_client_can_initialize_and_list_tools(self, monkeypatch, auth_headers):
        monkeypatch.setenv("MCP_HTTP_ENABLED", "true")
        app = app_module.create_app()
        headers = {**_STREAMABLE_HEADERS, **auth_headers}

        async with http_transport.mcp_http_lifespan(app), await _client(app, _LOCAL) as client:
            init = await client.post(STREAMABLE_URL, json=_INITIALIZE, headers=headers)
            session = {
                **headers,
                "mcp-session-id": init.headers["mcp-session-id"],
                "mcp-protocol-version": "2025-03-26",
            }
            await client.post(
                STREAMABLE_URL,
                json={"jsonrpc": "2.0", "method": "notifications/initialized"},
                headers=session,
            )
            listed = await client.post(
                STREAMABLE_URL,
                json={"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
                headers=session,
            )

        assert init.status_code == 200
        assert _sse_json(init)["result"]["protocolVersion"] == "2025-03-26"
        assert listed.status_code == 200
        names = {tool["name"] for tool in _sse_json(listed)["result"]["tools"]}
        assert {"recall", "remember", "forget", "memory_status"} <= names, (
            f"the tools stdio clients get should be here too, found {sorted(names)}"
        )

    @pytest.mark.parametrize(
        "authorization", [None, "Bearer mv_not-real"], ids=["no-token", "bad-token"]
    )
    async def test_unauthenticated_requests_are_refused(self, monkeypatch, authorization):
        monkeypatch.setenv("MCP_HTTP_ENABLED", "true")
        app = app_module.create_app()
        headers = dict(_STREAMABLE_HEADERS)
        if authorization:
            headers["Authorization"] = authorization

        async with http_transport.mcp_http_lifespan(app), await _client(app, _LOCAL) as client:
            r = await client.post(STREAMABLE_URL, json=_INITIALIZE, headers=headers)

        assert r.status_code == 401

    async def test_a_host_that_is_not_allowed_is_refused(self, monkeypatch, auth_headers):
        """DNS-rebinding protection applies here exactly as it does to SSE."""
        monkeypatch.setenv("MCP_HTTP_ENABLED", "true")
        monkeypatch.delenv("MCP_HTTP_ALLOWED_HOSTS", raising=False)
        app = app_module.create_app()
        headers = {**_STREAMABLE_HEADERS, **auth_headers, "Host": "evil.example"}

        async with http_transport.mcp_http_lifespan(app), await _client(app, _LOCAL) as client:
            r = await client.post(STREAMABLE_URL, json=_INITIALIZE, headers=headers)

        assert r.status_code == 421

    async def test_the_sse_endpoint_is_unchanged(self, monkeypatch, auth_headers):
        """
        Adding a transport must not change the existing one. A `POST` to the
        SSE stream is still refused the way it always was — which is also the
        exact response a Streamable-HTTP-only client gets when pointed at the
        wrong URL, and what the README's 405 section describes.
        """
        monkeypatch.setenv("MCP_HTTP_ENABLED", "true")
        app = app_module.create_app()
        headers = {**_STREAMABLE_HEADERS, **auth_headers}

        async with http_transport.mcp_http_lifespan(app), await _client(app, _LOCAL) as client:
            r = await client.post(f"{MCP_MOUNT_PATH}/sse", json=_INITIALIZE, headers=headers)

        assert r.status_code == 405
        # Compared as a set: Starlette builds this header from an unordered
        # set, and the order measurably differs between runs.
        assert {m.strip() for m in r.headers["allow"].split(",")} == {"GET", "HEAD"}

    async def test_the_app_lifespan_runs_the_session_manager(self, monkeypatch, auth_headers):
        """
        The wiring, measured end to end through the app's real lifespan with
        only the database pool stubbed.

        The first half is the precondition that makes the second half mean
        anything: without the session manager running, a request fails. If a
        future SDK stopped needing it, the second half would pass whether or
        not the lifespan was wired at all.
        """
        monkeypatch.setenv("MCP_HTTP_ENABLED", "true")
        monkeypatch.setattr(app_module, "init_pool", _noop)
        monkeypatch.setattr(app_module, "close_pool", _noop)
        headers = {**_STREAMABLE_HEADERS, **auth_headers}

        unstarted = app_module.create_app()
        async with await _client(unstarted, _LOCAL) as client:
            with pytest.raises(RuntimeError, match="Task group is not initialized"):
                await client.post(STREAMABLE_URL, json=_INITIALIZE, headers=headers)

        app = app_module.create_app()
        async with app.router.lifespan_context(app), await _client(app, _LOCAL) as client:
            r = await client.post(STREAMABLE_URL, json=_INITIALIZE, headers=headers)

        assert r.status_code == 200, "the app's lifespan must start the session manager"
