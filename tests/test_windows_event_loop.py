"""
Choosing an event loop psycopg can use, on Windows.

Windows defaults to `ProactorEventLoop`, and psycopg refuses to run on it —
`AsyncConnection.connect` raises `InterfaceError` before attempting any network
I/O. The pool reads that as a failed connection and retries; every retry raises
instantly. Start-up becomes a burst of identical warnings and a multi-second
delay before anything works.

Reproduced on Windows 11 with the versions this package pins (psycopg 3.3.5,
psycopg-pool 3.3.1):

    loop: ProactorEventLoop
    WARNING error connecting in 'pool-1': Psycopg cannot use the
      'ProactorEventLoop' to run in async mode. ...        (x3)
    OUTCOME: PoolTimeout pool initialization incomplete after 5 sec

and with the selector policy forced, on the same machine:

    loop: _WindowsSelectorEventLoop
    OUTCOME: PoolTimeout pool initialization incomplete after 5 sec

Zero guard errors. The remaining timeout is the probe pointing at a dead port
on purpose — what changed is *why* it failed.

These tests run on Linux in CI, where `WindowsSelectorEventLoopPolicy` does not
exist and the function is a no-op. So they assert on the decision the function
makes rather than on a policy it cannot set here: that it declines on this
platform, that it is wired into import, and that it leaves a caller's own
choice alone. The Windows behaviour itself was verified on Windows.
"""

from __future__ import annotations

import asyncio
import subprocess
import sys
import textwrap

import pytest

from memory_vault import _use_selector_event_loop_on_windows


class TestOnThisPlatform:
    @pytest.mark.skipif(sys.platform == "win32", reason="the non-Windows path")
    def test_it_declines_off_windows(self):
        assert _use_selector_event_loop_on_windows() is False

    @pytest.mark.skipif(sys.platform == "win32", reason="the non-Windows path")
    def test_the_policy_is_left_alone_off_windows(self):
        """
        Importing a library should not rearrange asyncio for everyone else.
        On Linux and macOS the default loop is already one psycopg is happy
        with, so there is nothing to fix and nothing to touch.
        """
        before = type(asyncio.get_event_loop_policy())

        _use_selector_event_loop_on_windows()

        assert type(asyncio.get_event_loop_policy()) is before

    @pytest.mark.skipif(sys.platform != "win32", reason="the Windows path")
    def test_it_selects_a_psycopg_compatible_loop_on_windows(self):  # pragma: no cover
        """Runs only on Windows; CI is Linux, so this is for a local run there."""
        policy = asyncio.get_event_loop_policy()
        assert isinstance(policy, asyncio.WindowsSelectorEventLoopPolicy)

        loop = policy.new_event_loop()
        try:
            assert "Proactor" not in type(loop).__name__
        finally:
            loop.close()


class TestItIsWiredIntoImport:
    """
    The loop has to be chosen before anything calls `asyncio.run`, so the call
    belongs at import rather than in one entry point. A fix that only ran under
    `memory-vault api` would leave the MCP server and the CLI broken.
    """

    def test_importing_the_package_applies_the_policy(self):
        """
        Observes the effect of importing, rather than reading the source for a
        call. An earlier version of this test grepped the file for
        `_use_selector_event_loop_on_windows()` and passed happily with the
        call deleted — the function's own `def` line contains that string.
        Found by mutation.

        The platform is faked inside a subprocess so this exercises the real
        Windows branch from Linux: `sys.platform` is patched and a stand-in
        policy class installed before the package is imported, then the policy
        is read back afterwards.
        """
        code = textwrap.dedent("""
            import asyncio, sys

            class FakeSelectorPolicy(asyncio.DefaultEventLoopPolicy):
                pass

            sys.platform = "win32"
            asyncio.WindowsSelectorEventLoopPolicy = FakeSelectorPolicy

            import memory_vault  # noqa: F401  - the import is the thing under test

            applied = isinstance(asyncio.get_event_loop_policy(), FakeSelectorPolicy)
            print("POLICY_APPLIED", applied)
        """)

        result = subprocess.run(  # noqa: S603
            [sys.executable, "-c", code], capture_output=True, text=True, timeout=120
        )

        assert result.returncode == 0, result.stderr
        assert "POLICY_APPLIED True" in result.stdout, (
            "importing the package must apply the policy on Windows; got: "
            f"{result.stdout!r} {result.stderr[-400:]!r}"
        )

    def test_it_is_applied_before_the_entry_points_are_defined(self):
        """
        Importing any module in the package is enough — the CLI, the API and
        the MCP server all import `memory_vault` first.
        """
        code = textwrap.dedent("""
            import memory_vault.cli  # noqa: F401
            import sys
            print("imported-without-error", sys.platform)
        """)

        result = subprocess.run(  # noqa: S603
            [sys.executable, "-c", code], capture_output=True, text=True, timeout=120
        )

        assert result.returncode == 0, result.stderr
        assert "imported-without-error" in result.stdout


class TestItDoesNotOverrideADeliberateChoice:
    """
    An application embedding this package may have chosen a policy for its own
    reasons — uvloop, or a custom one. Silently replacing it would be a worse
    failure than the warnings this avoids, and far harder to debug.
    """

    def test_a_custom_policy_is_left_in_place(self, monkeypatch):
        """
        The two classes must be *different*. An earlier version used one class
        for both the stand-in selector policy and the caller's policy, so the
        `isinstance(current, policy_cls)` check returned first and the guard
        under test never ran — the test passed with that guard deleted. Found
        by mutation.
        """

        class SelectorStandIn(asyncio.DefaultEventLoopPolicy):
            pass

        class SomebodyElsesPolicy(asyncio.DefaultEventLoopPolicy):
            """Not the default type, and not the selector one either."""

        monkeypatch.setattr(sys, "platform", "win32")
        monkeypatch.setattr(
            asyncio, "WindowsSelectorEventLoopPolicy", SelectorStandIn, raising=False
        )

        theirs = SomebodyElsesPolicy()
        monkeypatch.setattr(asyncio, "get_event_loop_policy", lambda: theirs)

        applied: list[object] = []
        monkeypatch.setattr(asyncio, "set_event_loop_policy", applied.append)

        changed = _use_selector_event_loop_on_windows()

        assert changed is False, "a caller's own policy must not be replaced"
        assert applied == [], f"it overwrote a deliberate choice: {applied}"

    def test_it_does_not_reapply_when_already_selector(self, monkeypatch):
        class FakeSelectorPolicy(asyncio.DefaultEventLoopPolicy):
            pass

        applied: list[object] = []

        monkeypatch.setattr(sys, "platform", "win32")
        monkeypatch.setattr(
            asyncio, "WindowsSelectorEventLoopPolicy", FakeSelectorPolicy, raising=False
        )
        monkeypatch.setattr(asyncio, "get_event_loop_policy", FakeSelectorPolicy)
        monkeypatch.setattr(asyncio, "set_event_loop_policy", applied.append)

        changed = _use_selector_event_loop_on_windows()

        assert changed is False
        assert applied == [], "setting it twice is pointless work at every import"


class TestTheReasonTheFixIsSafe:
    """
    The selector loop cannot run asyncio subprocesses. That is fine only
    because this package never starts one — checked here so a future change
    that adds `create_subprocess_exec` has to notice this trade-off rather
    than discover it on a user's Windows machine.
    """

    def test_no_module_uses_asyncio_subprocesses(self):
        from pathlib import Path

        src = Path(__import__("memory_vault").__file__).parent
        offenders = []
        for path in src.rglob("*.py"):
            text = path.read_text(encoding="utf-8")
            if "create_subprocess_exec" in text or "create_subprocess_shell" in text:
                offenders.append(path.name)

        assert not offenders, (
            f"asyncio subprocesses do not work under the selector loop this "
            f"package selects on Windows: {offenders}"
        )

    def test_diagnose_uses_the_synchronous_subprocess_api(self):
        """`subprocess.run` is unaffected by the event loop — verified on
        Windows, where it returned 42 under the selector policy."""
        from pathlib import Path

        import memory_vault

        diagnose = Path(memory_vault.__file__).parent / "diagnose.py"
        if not diagnose.exists():  # pragma: no cover - defensive
            pytest.skip("diagnose.py not present")

        text = diagnose.read_text(encoding="utf-8")
        if "subprocess" in text:
            assert "create_subprocess" not in text
