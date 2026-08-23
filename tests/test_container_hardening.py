"""
Container hardening properties.

These assert the shape of the Dockerfiles and the shipped compose file rather
than a running container — the runtime behaviour is verified by actually
booting the image, which does not belong in the unit suite. What these catch is
the silent regression: someone adds a RUN step after USER, or drops the
read_only flag while debugging, and nothing else notices.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parent.parent
DOCKERFILES = ["Dockerfile", "Dockerfile.mcp"]


def _lines(name: str) -> list[str]:
    return (REPO / name).read_text().splitlines()


@pytest.mark.parametrize("dockerfile", DOCKERFILES)
def test_image_switches_to_a_non_root_user(dockerfile):
    directives = [ln.strip() for ln in _lines(dockerfile) if ln.strip().startswith("USER ")]
    assert directives, f"{dockerfile} must switch to a non-root USER"
    assert not directives[-1].split()[1].startswith("root"), f"{dockerfile} ends up running as root"


@pytest.mark.parametrize("dockerfile", DOCKERFILES)
def test_nothing_runs_after_the_user_switch(dockerfile):
    """
    A RUN placed after USER either fails on a read-only path or quietly writes
    files owned by the wrong user. Keeping the switch last makes that ordering
    mistake impossible to introduce accidentally.
    """
    lines = [ln.strip() for ln in _lines(dockerfile)]
    user_at = max(i for i, ln in enumerate(lines) if ln.startswith("USER "))
    after = [ln for ln in lines[user_at + 1 :] if ln.startswith("RUN ")]
    assert after == [], f"{dockerfile} has RUN steps after USER: {after}"


@pytest.mark.parametrize("dockerfile", DOCKERFILES)
def test_model_cache_is_redirected_off_root_home(dockerfile):
    """
    HF_HOME defaults to /root/.cache, which a non-root user can neither read
    nor write. Without redirecting it the image starts and then fails on the
    first embedding call.
    """
    # Directives only — the comment above HF_HOME names /root/.cache to explain
    # what is being avoided, and matching prose would fail on the explanation
    # rather than on the configuration.
    directives = [
        ln.strip() for ln in _lines(dockerfile) if not ln.strip().startswith("#") and ln.strip()
    ]
    body = "\n".join(directives)
    assert "HF_HOME=" in body, f"{dockerfile} must set HF_HOME"
    assert "/root/.cache" not in body, f"{dockerfile} still points a cache at /root"


@pytest.mark.parametrize("dockerfile", DOCKERFILES)
def test_embedding_model_is_baked_in_at_build_time(dockerfile):
    """
    Without this the first request after every start downloads the model —
    which fails outright on a read-only root filesystem, and otherwise puts a
    network fetch in the path of the user's first query.
    """
    build_steps = [ln for ln in _lines(dockerfile) if ln.strip().startswith("RUN ")]
    assert any("SentenceTransformer(" in ln for ln in build_steps), (
        f"{dockerfile} must download the embedding model in a RUN step at build time"
    )


@pytest.fixture
def app_service():
    compose = yaml.safe_load((REPO / "docker-compose.yml").read_text())
    return compose["services"]["app"]


class TestComposeHardening:
    def test_root_filesystem_is_read_only(self, app_service):
        assert app_service.get("read_only") is True

    def test_all_capabilities_are_dropped(self, app_service):
        assert app_service.get("cap_drop") == ["ALL"]

    def test_privilege_escalation_is_blocked(self, app_service):
        assert "no-new-privileges:true" in (app_service.get("security_opt") or [])

    def test_tmp_is_writable_for_streamed_uploads(self, app_service):
        """
        File ingestion streams to a tempfile. With a read-only rootfs and no
        tmpfs, upload returns 500 while everything else keeps working — the
        kind of partial break that survives a smoke test.
        """
        tmpfs = app_service.get("tmpfs") or []
        assert any(entry.split(":")[0] == "/tmp" for entry in tmpfs), (
            "read_only rootfs requires a tmpfs at /tmp or file upload breaks"
        )

    def test_logs_still_have_a_writable_destination(self, app_service):
        volumes = app_service.get("volumes") or []
        assert any("/var/log/memory-vault" in v for v in volumes)
