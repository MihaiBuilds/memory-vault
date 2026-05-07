"""
Redaction sweep tests for `memory-vault diagnose`.

Discipline (never log content) is the primary defence — these tests verify
the secondary sweep that scrubs accidental leaks before zipping.
"""

from __future__ import annotations

import zipfile

from src.diagnose import (
    _collect_env,
    _redact_line,
    _redact_log_text,
    write_bundle,
)


class TestRedactLine:
    def test_bearer_token_redacted(self):
        line = "Authorization: Bearer mv_abc123XYZ_long_token_value"
        out = _redact_line(line)
        assert "mv_abc123XYZ_long_token_value" not in out
        assert "Bearer ***" in out

    def test_bearer_case_insensitive(self):
        line = "auth header: bearer ABC123def456"
        out = _redact_line(line)
        assert "ABC123def456" not in out
        assert "***" in out

    def test_mv_token_redacted_anywhere(self):
        line = "request used token mv_abcdefghij1234567890 to authenticate"
        out = _redact_line(line)
        assert "mv_abcdefghij1234567890" not in out
        assert "mv_***" in out

    def test_password_kv_in_dsn_redacted(self):
        line = "connecting to postgres://user:password=hunter2_secret@host/db"
        out = _redact_line(line)
        assert "hunter2_secret" not in out

    def test_json_password_field_redacted(self):
        line = '{"db_password": "hunter2", "host": "localhost"}'
        out = _redact_line(line)
        assert "hunter2" not in out
        assert "localhost" in out  # non-sensitive value preserved

    def test_api_key_redacted(self):
        line = "OPENAI_API_KEY=sk-proj-abc123xyz789"
        out = _redact_line(line)
        assert "sk-proj-abc123xyz789" not in out

    def test_safe_text_unchanged(self):
        line = 'INFO: ingested chunk 7d00ae23 into space "default"'
        out = _redact_line(line)
        assert out == line


class TestRedactLogText:
    def test_multiline_each_line_processed(self):
        text = "line one — Bearer abc123\nline two — clean\nline three — mv_xyz1234567890abc"
        out = _redact_log_text(text)
        assert "abc123" not in out
        assert "mv_xyz1234567890abc" not in out
        assert "line two — clean" in out


class TestCollectEnv:
    def test_password_env_redacted(self, monkeypatch):
        monkeypatch.setenv("DB_PASSWORD", "hunter2")
        monkeypatch.setenv("DB_HOST", "localhost")
        env = _collect_env()
        assert env["DB_PASSWORD"] == "***"
        assert env["DB_HOST"] == "localhost"

    def test_unrelated_env_filtered_out(self, monkeypatch):
        monkeypatch.setenv("HOME", "/Users/somebody")
        monkeypatch.setenv("PATH", "/usr/bin")
        env = _collect_env()
        assert "HOME" not in env
        assert "PATH" not in env

    def test_token_substring_redacted(self, monkeypatch):
        monkeypatch.setenv("API_SECRET_TOKEN", "supersecret")
        env = _collect_env()
        assert env["API_SECRET_TOKEN"] == "***"


class TestWriteBundle:
    def test_zip_contains_expected_files(self, tmp_path, monkeypatch):
        # Isolate from real env / cwd
        monkeypatch.setenv("LOG_FILE", str(tmp_path / "no-such-log.jsonl"))
        zip_path = write_bundle(out_dir=tmp_path)
        assert zip_path.exists()
        assert zip_path.suffix == ".zip"
        with zipfile.ZipFile(zip_path) as zf:
            names = set(zf.namelist())
        expected = {
            "manifest.json",
            "app.jsonl",
            "status.txt",
            "system.txt",
            "env.txt",
            "docker.txt",
        }
        assert expected.issubset(names)

    def test_zip_filename_pattern(self, tmp_path):
        zip_path = write_bundle(out_dir=tmp_path)
        assert zip_path.name.startswith("memory-vault-diagnostic-")
        assert zip_path.name.endswith(".zip")

    def test_app_log_content_redacted_in_bundle(self, tmp_path, monkeypatch):
        # Plant a fake log file with a leaking bearer token
        log_file = tmp_path / "fake.jsonl"
        log_file.write_text(
            '{"event": "request", "auth": "Bearer mv_leaktoken123456"}\n'
            '{"event": "ok", "chunk_id": "abc"}\n'
        )
        monkeypatch.setenv("LOG_FILE", str(log_file))
        zip_path = write_bundle(out_dir=tmp_path)
        with zipfile.ZipFile(zip_path) as zf:
            log_content = zf.read("app.jsonl").decode()
        assert "mv_leaktoken123456" not in log_content
        assert "Bearer ***" in log_content
        assert "chunk_id" in log_content  # safe identifier preserved
