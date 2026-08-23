"""
Uploaded files record the name the user sent, not the server's tempfile.

The upload route streams to a `NamedTemporaryFile` and passed that path to the
pipeline, so every chunk from an upload stored a `source_file` like
`/tmp/tmpabcd1234.md` — a path that stops existing the moment the request ends.

The route already validated and kept `file.filename`; it just never reached the
adapter. The pipeline now carries an optional `source_name` that overrides what
gets recorded, while detection still reads the real path because it needs the
extension of the file actually on disk.
"""

from __future__ import annotations

import io

import pytest

from memory_vault.models.db import fetch_all

pytestmark = pytest.mark.asyncio


async def _upload(client, auth_headers, name: str, body: str, space: str):
    return await client.post(
        "/api/ingest/file",
        files={"file": (name, io.BytesIO(body.encode()), "text/markdown")},
        data={"space": space},
        headers=auth_headers,
    )


async def _sources(space: str) -> list[str]:
    rows = await fetch_all(
        """SELECT DISTINCT c.metadata->>'source_file' AS sf
           FROM chunks c JOIN memory_spaces ms ON ms.id = c.space_id
           WHERE ms.name = %s""",
        (space,),
    )
    return sorted(r["sf"] for r in rows if r["sf"])


class TestUploadedSourceIsTheOriginalName:
    async def test_source_is_the_uploaded_filename(self, client, auth_headers):
        await client.post("/api/spaces", json={"name": "up1"}, headers=auth_headers)
        resp = await _upload(
            client, auth_headers, "notes.md", "# Notes\n\nSome content here.", "up1"
        )
        assert resp.status_code == 200

        assert await _sources("up1") == ["notes.md"]

    async def test_source_is_not_a_temporary_path(self, client, auth_headers):
        """The specific symptom: a path under /tmp that no longer exists."""
        await client.post("/api/spaces", json={"name": "up2"}, headers=auth_headers)
        await _upload(client, auth_headers, "report.md", "# Report\n\nBody text.", "up2")

        for source in await _sources("up2"):
            assert not source.startswith("/tmp/"), f"{source} is a server tempfile"
            assert "tmp" not in source.lower(), f"{source} looks like a tempfile"

    async def test_two_uploads_of_the_same_file_do_not_duplicate(self, client, auth_headers):
        """
        Migration 005 keys ingest identity on
        (space_id, source_file, chunk_index, content_hash). A tempfile path is
        different every request, so re-uploading the same file used to store a
        second copy. A stable source name makes that identity work as intended.
        """
        await client.post("/api/spaces", json={"name": "up3"}, headers=auth_headers)
        body = "# Stable\n\nIdentical on both uploads."

        first = await _upload(client, auth_headers, "stable.md", body, "up3")
        second = await _upload(client, auth_headers, "stable.md", body, "up3")
        assert first.status_code == 200
        assert second.status_code == 200

        rows = await fetch_all(
            """SELECT count(*) AS c FROM chunks c
               JOIN memory_spaces ms ON ms.id = c.space_id
               WHERE ms.name = %s""",
            ("up3",),
        )
        first_count = first.json()["chunks_created"]
        assert rows[0]["c"] == first_count, "re-uploading an unchanged file should not add rows"

    async def test_different_files_keep_their_own_sources(self, client, auth_headers):
        await client.post("/api/spaces", json={"name": "up4"}, headers=auth_headers)
        await _upload(client, auth_headers, "alpha.md", "# Alpha\n\nOne.", "up4")
        await _upload(client, auth_headers, "beta.md", "# Beta\n\nTwo.", "up4")

        assert await _sources("up4") == ["alpha.md", "beta.md"]


class TestOtherIngestPathsAreUnchanged:
    """
    Only uploads have a tempfile problem. CLI and directory ingestion read a
    real path the user chose, and that path is the durable identifier — the
    override must not disturb them.
    """

    async def test_pipeline_without_source_name_uses_the_file_path(self, tmp_path):
        from memory_vault.services.ingestion import IngestionJob

        job = IngestionJob(priority=1, file_path="/data/notes.md", space_id=1)
        assert job.source_name is None, "default must leave the file path in charge"

    async def test_text_ingest_still_records_its_own_source(self, client, auth_headers):
        """`POST /api/ingest/text` has no file at all and must be untouched."""
        await client.post("/api/spaces", json={"name": "up5"}, headers=auth_headers)
        resp = await client.post(
            "/api/ingest/text",
            json={"text": "A memory with no file behind it.", "space": "up5"},
            headers=auth_headers,
        )
        assert resp.status_code == 200

        for source in await _sources("up5"):
            assert not source.startswith("/tmp/")
