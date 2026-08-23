"""
Environment settings that are present but empty.

`os.getenv(name, default)` falls back to the default only when the key is
absent. An empty value is a legitimate value and passed straight through to
`int()`, so `DB_PORT=""` raised `ValueError: invalid literal for int()` while
importing `config.py` — upstream of the health endpoint, the pool's retries
and the startup probes, so nothing could report it.

Config generated from a manifest emits every declared key, empty where the
generator has no value to supply, which makes present-but-empty the normal
shape of machine-written config rather than an edge case.
"""

from __future__ import annotations

import os
import subprocess
import sys

import pytest

from memory_vault.config import InvalidSetting, env_int, env_str

NUMERIC_SETTINGS = [
    ("DB_PORT", "db_port", 5432),
    ("API_PORT", "api_port", 8000),
    ("EMBEDDING_DIMENSIONS", "embedding_dimensions", 384),
    ("EMBEDDING_BATCH_SIZE", "embedding_batch_size", 32),
    ("RRF_K", "rrf_k", 60),
    ("SEARCH_DEFAULT_LIMIT", "search_default_limit", 10),
]

_ATTR_FOR = {var: attr for var, attr, _ in NUMERIC_SETTINGS}


def _import_config_with(**env: str) -> subprocess.CompletedProcess[str]:
    """Import config in a fresh process with `env` applied, printing one setting.

    The import has to happen after the environment is set, which rules out
    monkeypatch: the dataclass captures its defaults when the class is defined.
    """
    (var,) = env
    child_env = {**os.environ, **env}
    return subprocess.run(  # nosec B603 — fixed argv, no shell, test-only
        [
            sys.executable,
            "-c",
            f"from memory_vault.config import settings; print(settings.{_ATTR_FOR[var]})",
        ],
        env=child_env,
        capture_output=True,
        text=True,
        timeout=60,
    )


class TestEmptyIsTreatedAsUnset:
    @pytest.mark.parametrize("empty", ["", "   ", "\t", "\n"])
    def test_env_int_falls_back_on_empty(self, monkeypatch, empty):
        monkeypatch.setenv("SOME_PORT", empty)
        assert env_int("SOME_PORT", 5432) == 5432

    @pytest.mark.parametrize("empty", ["", "   "])
    def test_env_str_falls_back_on_empty(self, monkeypatch, empty):
        monkeypatch.setenv("SOME_HOST", empty)
        assert env_str("SOME_HOST", "localhost") == "localhost"

    def test_absent_key_still_falls_back(self, monkeypatch):
        monkeypatch.delenv("SOME_PORT", raising=False)
        assert env_int("SOME_PORT", 7) == 7
        assert env_str("SOME_HOST", "here") == "here"

    @pytest.mark.parametrize("var,attr,default", NUMERIC_SETTINGS)
    def test_every_numeric_setting_survives_an_empty_value(self, var, attr, default):
        """
        The regression itself, exercised the way a user meets it.

        `Settings` is a frozen dataclass, so its field defaults are evaluated
        once when the class is defined — at import. `monkeypatch.setenv` after
        that cannot change them, and a test using it would pass whatever the
        code did. A subprocess is the only honest check here, and it also
        matches the real failure: the process died while importing config.
        """
        result = _import_config_with(**{var: ""})
        assert result.returncode == 0, f"{var}='' must not crash the import:\n{result.stderr}"
        assert result.stdout.strip() == str(default), (
            f"{var}='' should fall back to {default}, got {result.stdout.strip()!r}"
        )


class TestRealValuesStillWin:
    def test_env_int_reads_a_real_value(self, monkeypatch):
        monkeypatch.setenv("SOME_PORT", "6543")
        assert env_int("SOME_PORT", 5432) == 6543

    def test_surrounding_whitespace_is_tolerated(self, monkeypatch):
        """`DB_PORT=" 5432 "` from a generated .env should not be fatal."""
        monkeypatch.setenv("SOME_PORT", "  6543  ")
        assert env_int("SOME_PORT", 5432) == 6543

    def test_env_str_reads_a_real_value(self, monkeypatch):
        monkeypatch.setenv("SOME_HOST", "db.internal")
        assert env_str("SOME_HOST", "localhost") == "db.internal"

    @pytest.mark.parametrize("var,attr,_default", NUMERIC_SETTINGS)
    def test_settings_still_read_configured_values(self, var, attr, _default):
        """Also a subprocess, for the reason given above."""
        result = _import_config_with(**{var: "123"})
        assert result.returncode == 0, result.stderr
        assert result.stdout.strip() == "123", (
            f"{var}=123 should be read, got {result.stdout.strip()!r}"
        )


class TestMalformedValuesFailClearly:
    @pytest.mark.parametrize("bad", ["abc", "5432x", "12.5", "port"])
    def test_malformed_int_names_the_setting(self, monkeypatch, bad):
        """
        A genuinely wrong value should still fail — but with the name of the
        setting and what it received, not a bare ValueError from inside int().
        """
        monkeypatch.setenv("SOME_PORT", bad)
        with pytest.raises(InvalidSetting) as exc:
            env_int("SOME_PORT", 5432)

        message = str(exc.value)
        assert "SOME_PORT" in message, "the message must name the setting"
        assert repr(bad) in message, "the message must show what was received"
        assert "5432" in message, "the message must state the default"

    def test_invalid_setting_is_a_valueerror(self):
        """
        Callers that already catch ValueError keep working; the subclass only
        adds a better message.
        """
        assert issubclass(InvalidSetting, ValueError)
