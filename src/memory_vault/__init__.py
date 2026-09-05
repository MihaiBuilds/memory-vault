"""Memory Vault package."""

import asyncio
import sys
from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("memory-vault")
except PackageNotFoundError:
    __version__ = "unknown"


def _use_selector_event_loop_on_windows() -> bool:
    """Make asyncio use a loop psycopg can actually talk to, on Windows.

    Windows defaults to ``ProactorEventLoop``, and psycopg refuses to run on
    it: ``AsyncConnection.connect`` raises ``InterfaceError`` before any
    network I/O happens. The pool reads that as a failed connection and
    retries, and every retry raises instantly — so a start-up that should be
    immediate becomes a burst of identical warnings and a several-second delay
    before anything works. Reproduced on Windows 11 with the versions this
    package pins: three warnings inside a five-second window, then
    ``PoolTimeout``.

    Forcing the selector loop costs nothing here. The one thing it cannot do
    is asyncio subprocesses, and this package never starts one: ``diagnose``
    uses the synchronous ``subprocess.run``, and the MCP server reads stdio
    directly rather than spawning anything. Both were confirmed on Windows
    rather than only by reading the source.

    Set at import so every entry point inherits it — CLI, API and MCP server
    alike — because the loop has to be chosen before anyone calls
    ``asyncio.run``. It only ever touches Windows: on every other platform
    this returns immediately, and ``WindowsSelectorEventLoopPolicy`` does not
    even exist there.

    Returns whether the policy was changed, which is what makes this testable
    on a non-Windows machine.
    """
    if sys.platform != "win32":  # pragma: no cover - platform-specific
        return False

    policy_cls = getattr(asyncio, "WindowsSelectorEventLoopPolicy", None)
    if policy_cls is None:  # pragma: no cover - defensive, Windows always has it
        return False

    # Leave a deliberate choice alone. An embedding application that has
    # already selected a policy knows something about its own needs that this
    # import does not, and silently overriding it would be worse than the
    # warnings this avoids.
    current = asyncio.get_event_loop_policy()
    if isinstance(current, policy_cls):
        return False
    if type(current) is not asyncio.DefaultEventLoopPolicy:
        return False

    asyncio.set_event_loop_policy(policy_cls())
    return True


_use_selector_event_loop_on_windows()

__all__ = ["__version__"]
