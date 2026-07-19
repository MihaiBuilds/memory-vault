"""
`memory-vault diagnose` — bundle everything needed for a bug report into a
single redacted zip the user can attach to a GitHub issue.

Two run modes:
  - In-container: read /var/log/memory-vault/app.jsonl + env + status.
    Skips docker-related collection with a note.
  - On-host:      additionally shells out to `docker compose ps` and
    `docker compose logs db` so the bundle is complete.

Privacy: the redaction sweep is the safety net, not the only line of defence.
The structured-logging discipline (chunk_id only, never chunk content) is the
primary protection — see src/logging_config.py docstring.
"""

from __future__ import annotations

import json
import os
import platform
import re
import shutil
import socket
import subprocess
import sys
import zipfile
from datetime import UTC, datetime
from pathlib import Path

from memory_vault.logging_config import get_log_file

APP_LOG_TAIL_LINES = 1000
DB_LOG_TAIL_LINES = 500

_REDACT_ENV_KEYS = {
    "DB_PASSWORD",
    "POSTGRES_PASSWORD",
    "API_TOKEN",
    "MV_TOKEN",
    "OPENAI_API_KEY",
    "ANTHROPIC_API_KEY",
}

# Substrings that flag a key as sensitive even if not in the explicit allowlist.
_REDACT_KEY_SUBSTRINGS = ("password", "secret", "token", "api_key", "apikey")

# Regex sweeps applied line-by-line over log content.
_BEARER_RE = re.compile(r"(Bearer\s+)[A-Za-z0-9_\-\.]+", re.IGNORECASE)
_MV_TOKEN_RE = re.compile(r"mv_[A-Za-z0-9]{10,}")
_PASSWORD_KV_RE = re.compile(
    r"((?:password|secret|token|api[_-]?key)[\"']?\s*[=:]\s*[\"']?)([^\"'\s,}]+)",
    re.IGNORECASE,
)


def _redact_line(line: str) -> str:
    line = _BEARER_RE.sub(r"\1***", line)
    line = _MV_TOKEN_RE.sub("mv_***", line)
    line = _PASSWORD_KV_RE.sub(r"\1***", line)
    return line


def _redact_log_text(text: str) -> str:
    return "\n".join(_redact_line(ln) for ln in text.splitlines())


def _tail(path: Path, n: int) -> str:
    """Read the last n lines of a file. Falls back to whole file for small files."""
    if not path.exists():
        return ""
    with path.open("rb") as f:
        f.seek(0, os.SEEK_END)
        size = f.tell()
        # Heuristic: read at most ~1MB from the end and split.
        read_size = min(size, 1_000_000)
        f.seek(size - read_size)
        chunk = f.read().decode("utf-8", errors="replace")
    lines = chunk.splitlines()
    return "\n".join(lines[-n:])


def _collect_env() -> dict[str, str]:
    """Return env vars relevant to Memory Vault, with sensitive values redacted."""
    relevant_prefixes = ("DB_", "API_", "LOG_", "EMBEDDING_", "SEARCH_", "RRF_", "POSTGRES_")
    out: dict[str, str] = {}
    for key, value in os.environ.items():
        if not key.startswith(relevant_prefixes):
            continue
        lower = key.lower()
        if key in _REDACT_ENV_KEYS or any(s in lower for s in _REDACT_KEY_SUBSTRINGS):
            out[key] = "***"
        else:
            out[key] = value
    return out


def _read_version() -> str:
    pyproject = Path(__file__).resolve().parent.parent / "pyproject.toml"
    if not pyproject.exists():
        return "unknown"
    try:
        for line in pyproject.read_text().splitlines():
            line = line.strip()
            if line.startswith("version"):
                # version = "0.4.0"
                return line.split("=", 1)[1].strip().strip('"').strip("'")
    except OSError:
        # pyproject.toml unreadable — return "unknown" rather than crash diagnostics.
        pass
    return "unknown"


def _run(cmd: list[str], timeout: float = 10.0) -> tuple[int, str]:
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
        out = result.stdout + ("\n" + result.stderr if result.stderr else "")
        return result.returncode, out
    except FileNotFoundError:
        return -1, f"(command not found: {cmd[0]})"
    except subprocess.TimeoutExpired:
        return -1, f"(command timed out after {timeout}s: {' '.join(cmd)})"
    except OSError as e:
        return -1, f"(command failed: {e})"


def _has_docker() -> bool:
    return shutil.which("docker") is not None


def _in_container() -> bool:
    return Path("/.dockerenv").exists()


def _run_status() -> str:
    """
    Run `memory-vault status` and capture its stdout.

    Uses a subprocess rather than calling `_cmd_status` in-process: the CLI
    function manages its own connection-pool lifecycle (init + close), and
    invoking it from a long-running process (or from inside a test that
    already owns a session-wide pool) would tear that pool down. A subprocess
    gets its own pool and leaves the parent's untouched.
    """
    rc, out = _run(["memory-vault", "status"], timeout=15.0)
    if rc != 0 and not out.strip():
        return f"(status failed: exit code {rc})\n"
    return out


def collect_bundle() -> dict[str, str]:
    """Assemble all sections of the diagnostic bundle as {filename: text}."""
    now = datetime.now(UTC).isoformat()
    sections: dict[str, str] = {}

    # 0. Manifest
    sections["manifest.json"] = json.dumps(
        {
            "memory_vault_version": _read_version(),
            "generated_at_utc": now,
            "hostname": socket.gethostname(),
            "in_container": _in_container(),
            "log_tail_lines": APP_LOG_TAIL_LINES,
            "db_log_tail_lines": DB_LOG_TAIL_LINES,
        },
        indent=2,
    )

    # 1. App logs (redacted)
    log_file = get_log_file()
    if log_file is not None:
        sections["app.jsonl"] = _redact_log_text(_tail(log_file, APP_LOG_TAIL_LINES))
    else:
        sections["app.jsonl"] = (
            "(no app log file found — set LOG_FILE or ensure logs/app.jsonl exists)\n"
        )

    # 2. status output
    sections["status.txt"] = _run_status()

    # 3. OS / runtime info
    sections["system.txt"] = "\n".join(
        [
            f"platform: {platform.platform()}",
            f"machine: {platform.machine()}",
            f"python: {sys.version.split()[0]}",
            f"executable: {sys.executable}",
            f"cwd: {Path.cwd()}",
            f"hostname: {socket.gethostname()}",
            f"in_container: {_in_container()}",
        ]
    )

    # 4. Env (filtered + redacted)
    env_lines = [f"{k}={v}" for k, v in sorted(_collect_env().items())]
    sections["env.txt"] = "\n".join(env_lines) if env_lines else "(no relevant env vars set)"

    # 5. Docker info — only if docker is on PATH AND we're not inside the container
    docker_section = []
    if _has_docker() and not _in_container():
        rc1, ps_out = _run(["docker", "compose", "ps"])
        docker_section.append(f"$ docker compose ps  (rc={rc1})\n{ps_out}")
        rc2, db_logs = _run(
            ["docker", "compose", "logs", "--no-color", "--tail", str(DB_LOG_TAIL_LINES), "db"],
            timeout=20.0,
        )
        docker_section.append(
            f"\n$ docker compose logs --tail {DB_LOG_TAIL_LINES} db  (rc={rc2})\n"
            + _redact_log_text(db_logs)
        )
    elif _in_container():
        docker_section.append(
            "(skipped — diagnose ran inside the container, no visibility into other "
            "services. Re-run from the host with `memory-vault diagnose` for the "
            "complete bundle.)"
        )
    else:
        docker_section.append("(skipped — `docker` not on PATH)")
    sections["docker.txt"] = "\n".join(docker_section)

    return sections


def write_bundle(out_dir: Path | None = None) -> Path:
    """Write the bundle to a timestamped zip and return the path."""
    out_dir = out_dir or Path.cwd()
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y-%m-%d-%H%M%S")
    zip_path = out_dir / f"memory-vault-diagnostic-{stamp}.zip"

    sections = collect_bundle()
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, text in sections.items():
            zf.writestr(name, text)

    return zip_path


def cli_diagnose(out_dir: Path | None = None) -> None:
    path = write_bundle(out_dir)
    size_kb = path.stat().st_size / 1024
    print(f"Diagnostic bundle written: {path}  ({size_kb:.1f} KB)")
    print("Contents are auto-redacted (bearer tokens, passwords, mv_ tokens).")
    print("Please review before posting it to a public GitHub issue.")
