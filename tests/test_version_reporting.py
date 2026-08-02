"""Version-reporting agreement.

Every surface that reports a Memory Vault version must agree with the
canonical installed-package version. Regression guard for #97.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from memory_vault import __version__

REPO_ROOT = Path(__file__).resolve().parent.parent


def test_installed_version_is_a_real_semver_string():
    """__version__ resolves via importlib.metadata rather than falling back to 'unknown'."""
    assert __version__ != "unknown", (
        "memory_vault.__version__ resolved to 'unknown' — the package is not installed "
        "in the test environment; run `pip install -e .` first."
    )
    assert re.fullmatch(r"\d+\.\d+\.\d+(?:[.\-+].+)?", __version__), (
        f"__version__ {__version__!r} is not a semver-shaped string"
    )


def test_pyproject_version_matches_installed():
    """pyproject.toml is the source of truth — importlib.metadata must reflect it."""
    text = (REPO_ROOT / "pyproject.toml").read_text()
    match = re.search(r'^version\s*=\s*"([^"]+)"', text, re.MULTILINE)
    assert match is not None, "pyproject.toml has no version line"
    assert match.group(1) == __version__


def test_docker_compose_app_image_tag_matches_installed():
    text = (REPO_ROOT / "docker-compose.yml").read_text()
    match = re.search(r"ghcr\.io/mihaibuilds/memory-vault:([^\s]+)", text)
    assert match is not None, "docker-compose.yml has no memory-vault image reference"
    assert match.group(1) == __version__


def test_server_json_version_matches_installed():
    data = json.loads((REPO_ROOT / "server.json").read_text())
    assert data["version"] == __version__


def test_server_json_mcp_image_tag_matches_installed():
    data = json.loads((REPO_ROOT / "server.json").read_text())
    identifier = data["packages"][0]["identifier"]
    assert identifier.endswith(f":{__version__}"), (
        f"server.json packages[0].identifier is {identifier!r}, expected suffix :{__version__}"
    )


def test_fastapi_openapi_version_matches_installed(app):
    """The FastAPI app's OpenAPI-reported version is the runtime surface users hit at /openapi.json."""
    assert app.version == __version__


@pytest.mark.asyncio
async def test_health_endpoint_reports_installed_version(client):
    """/api/health returns version=__version__ — the endpoint most-often scraped by monitors."""
    response = await client.get("/api/health")
    body = response.json()
    assert body["version"] == __version__


def test_diagnose_read_version_matches_installed():
    """The diagnose bundle's manifest reports the real version, not 'unknown'."""
    from memory_vault.diagnose import _read_version

    assert _read_version() == __version__
