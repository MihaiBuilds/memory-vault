"""
Uploading several files in one request.

`/api/ingest/file` took one file per request, so importing a folder of notes
meant one request per note. `/api/ingest/files` takes a batch.

The interesting part is not the loop, it is what a batch makes possible that a
single upload does not:

**Partial success.** One malformed file in a batch of thirty must not discard
the other twenty-nine, and the caller has to learn which one to fix. So the
response reports per file and the request itself succeeds whenever it was
well-formed.

**Leaking the tempfile path.** The pipeline records failures as
`"<path>: <message>"`, and that path is a tempfile the caller never named.
Measured before writing this: a forced failure produced
`/tmp/does-not-exist-at-all.md: [Errno 2] ...`. Returning those verbatim would
publish server filesystem layout and tell the caller nothing — the same shape
as #101, which put a tempfile path in a chunk's `source`. Errors are mapped
back to the uploaded filename.

**Three separate limits.** Per file, in aggregate, and by count. Any one alone
leaves a way to spend all the disk available: fifty 24 MB files pass a per-file
cap, and ten thousand tiny ones pass a byte cap.
"""

from __future__ import annotations

import pytest

from memory_vault.api.routers.ingest import (
    MAX_BATCH_BYTES,
    MAX_BATCH_FILES,
    MAX_UPLOAD_BYTES,
)


def _file(name: str, body: bytes = b"# Notes\n\nAlice shipped the release.") -> tuple:
    return ("files", (name, body, "text/markdown"))


async def _post(client, auth_headers, files: list, space: str = "default"):
    return await client.post(
        "/api/ingest/files", files=files, data={"space": space}, headers=auth_headers
    )


class TestABatchIsIngested:
    async def test_several_files_are_stored(self, client, auth_headers):
        r = await _post(
            client,
            auth_headers,
            [
                _file("one.md", b"# One\n\nAlice shipped the release."),
                _file("two.md", b"# Two\n\nBob reviewed the change."),
            ],
            space="bi1",
        )

        assert r.status_code == 200, r.text
        body = r.json()
        assert body["files_succeeded"] == 2
        assert body["files_failed"] == 0
        assert body["chunks_created"] >= 2

    async def test_every_file_gets_its_own_result(self, client, auth_headers):
        r = await _post(
            client, auth_headers, [_file("a.md"), _file("b.md"), _file("c.md")], space="bi2"
        )

        results = r.json()["files"]
        assert len(results) == 3
        assert {x["filename"] for x in results} == {"a.md", "b.md", "c.md"}
        assert all(x["stored"] for x in results)
        assert all(x["error"] is None for x in results)

    async def test_a_single_file_batch_works(self, client, auth_headers):
        r = await _post(client, auth_headers, [_file("solo.md")], space="bi3")

        assert r.status_code == 200, r.text
        assert r.json()["files_succeeded"] == 1

    async def test_the_space_is_created_on_first_write(self, client, auth_headers):
        """Same as the single-file path — a batch should not need the space
        to exist already."""
        r = await _post(client, auth_headers, [_file("x.md")], space="brand-new-space")
        assert r.status_code == 200, r.text

        listing = await client.get("/api/spaces", headers=auth_headers)
        assert "brand-new-space" in [s["name"] for s in listing.json()["spaces"]]

    async def test_an_invalid_space_name_is_rejected(self, client, auth_headers):
        r = await _post(client, auth_headers, [_file("x.md")], space="Not Valid")
        assert r.status_code == 400, r.text


class TestSourceNamesSurviveTheTempfile:
    """
    #101 in a new form. Each upload is spooled to a tempfile that is deleted
    when the request ends; without the `source_name` override every chunk in
    the batch would record a path that no longer exists.
    """

    async def test_chunks_record_the_uploaded_filename(self, client, auth_headers):
        await _post(
            client,
            auth_headers,
            [
                _file("meeting-notes.md", b"# Meeting\n\nAlice shipped the release."),
                _file("retro.md", b"# Retro\n\nBob reviewed the change."),
            ],
            space="bs1",
        )

        listing = await client.get("/api/chunks?space=bs1", headers=auth_headers)
        sources = {c["source"] for c in listing.json()["chunks"]}

        assert sources == {"meeting-notes.md", "retro.md"}, sources

    async def test_no_chunk_records_a_temporary_path(self, client, auth_headers):
        await _post(client, auth_headers, [_file("real-name.md")], space="bs2")

        listing = await client.get("/api/chunks?space=bs2", headers=auth_headers)
        for chunk in listing.json()["chunks"]:
            source = chunk["source"] or ""
            assert "/tmp/" not in source and "/var/folders" not in source, source
            assert not source.startswith("/"), f"source should be a filename, got {source}"


class TestPartialSuccess:
    async def test_one_bad_file_does_not_discard_the_good_ones(self, client, auth_headers):
        r = await _post(
            client,
            auth_headers,
            [
                _file("good.md", b"# Good\n\nAlice shipped the release."),
                _file("empty.md", b""),
                _file("also-good.md", b"# Also\n\nBob reviewed the change."),
            ],
            space="bp1",
        )

        assert r.status_code == 200, "a batch with one bad file is still a valid request"
        body = r.json()
        assert body["files_succeeded"] == 2
        assert body["files_failed"] == 1

        listing = await client.get("/api/chunks?space=bp1", headers=auth_headers)
        assert listing.json()["total"] >= 2, "the good files must have been stored"

    async def test_the_failing_file_is_named(self, client, auth_headers):
        """ "Something failed" would leave the caller re-uploading everything."""
        r = await _post(
            client, auth_headers, [_file("fine.md"), _file("broken.md", b"")], space="bp2"
        )

        by_name = {x["filename"]: x for x in r.json()["files"]}
        assert by_name["broken.md"]["stored"] is False
        assert by_name["broken.md"]["error"]
        assert by_name["fine.md"]["stored"] is True

    async def test_an_empty_file_is_reported_not_silently_skipped(self, client, auth_headers):
        r = await _post(client, auth_headers, [_file("nothing.md", b"")], space="bp3")

        body = r.json()
        assert body["files_failed"] == 1
        assert "empty" in body["files"][0]["error"].lower()

    async def test_the_message_mentions_failures(self, client, auth_headers):
        r = await _post(client, auth_headers, [_file("ok.md"), _file("bad.md", b"")], space="bp4")
        assert "failed" in r.json()["message"]


class TestTempPathsNeverReachTheCaller:
    """
    The leak this endpoint has to avoid, and the reason failures are not
    passed through verbatim.
    """

    async def test_no_response_field_contains_a_temporary_path(self, client, auth_headers):
        import json

        r = await _post(
            client,
            auth_headers,
            [_file("good.md"), _file("empty.md", b""), _file("../evil.md", b"x")],
            space="bt1",
        )

        body = json.dumps(r.json())
        assert "/tmp/" not in body, body
        assert "/var/folders" not in body, body
        assert "NamedTemporary" not in body

    async def test_errors_are_about_the_uploaded_name(self, client, auth_headers):
        r = await _post(client, auth_headers, [_file("mine.md", b"")], space="bt2")

        result = r.json()["files"][0]
        assert result["filename"] == "mine.md"
        assert result["error"] is not None
        assert ".md:" not in result["error"], (
            f"the raw '<path>: <message>' form leaked through: {result['error']}"
        )

    # The cases above are all rejected before the pipeline runs, so they never
    # populate `stats.errors` — the very place the tempfile path comes from.
    # These two reach the pipeline and fail inside it, which is the only path
    # that exercises the mapping. Found by mutation: passing the raw error
    # through left every test above still green.
    @pytest.mark.parametrize(
        ("name", "body"),
        [
            # NUL bytes survive the adapter and fail at insert.
            ("binary.md", b"Alice shipped\x00the release."),
            # Shaped like a Claude export, wrong inside.
            ("export.json", b'{"chat_messages": "not-a-list"}'),
        ],
    )
    async def test_a_pipeline_failure_does_not_leak_its_temp_path(
        self, client, auth_headers, name, body
    ):
        import json

        r = await _post(client, auth_headers, [_file(name, body)], space="bt3")

        assert r.status_code == 200, r.text
        result = r.json()["files"][0]
        assert result["stored"] is False, "precondition: this file must fail inside the pipeline"
        assert result["filename"] == name

        error = result["error"] or ""
        assert "/tmp/" not in error, f"tempfile path leaked into the error: {error}"
        assert "/var/folders" not in error, error
        assert not error.startswith("/"), error
        assert "/tmp/" not in json.dumps(r.json())

    async def test_a_pipeline_failure_still_reports_something_useful(self, client, auth_headers):
        """
        Stripping the path must not strip the reason — a bare "failed" would
        leave the caller with nothing to act on.
        """
        r = await _post(client, auth_headers, [_file("binary.md", b"Alice\x00Bob")], space="bt4")

        error = r.json()["files"][0]["error"]
        assert error, "the failure needs a message"
        assert len(error) > 10, f"message is too thin to act on: {error!r}"


class TestBadFilenames:
    @pytest.mark.parametrize(
        "name", ["../escape.md", "../../etc/passwd", "dir/nested.md", "back\\slash.md", ".hidden"]
    )
    async def test_traversal_and_separators_are_refused(self, client, auth_headers, name):
        """
        The path on disk is a tempfile, so there is no real escape — but the
        name is echoed back and stored as the chunk's source.
        """
        r = await _post(client, auth_headers, [_file(name, b"content")], space="bf1")

        assert r.status_code == 200
        assert r.json()["files_failed"] == 1
        assert r.json()["files_succeeded"] == 0

    async def test_a_bad_name_does_not_stop_the_rest(self, client, auth_headers):
        r = await _post(
            client, auth_headers, [_file("../evil.md", b"x"), _file("fine.md")], space="bf2"
        )

        body = r.json()
        assert body["files_succeeded"] == 1
        assert body["files_failed"] == 1


class TestLimits:
    async def test_the_three_caps_are_distinct(self):
        """
        Each closes a hole the others leave open: many small files pass a byte
        cap, and a few large ones pass a count cap.
        """
        assert MAX_UPLOAD_BYTES == 25 * 1024 * 1024
        assert MAX_BATCH_BYTES == 100 * 1024 * 1024
        assert MAX_BATCH_FILES == 100
        assert MAX_BATCH_BYTES > MAX_UPLOAD_BYTES

    async def test_too_many_files_is_refused_outright(self, client, auth_headers):
        """A count this far over the line is rejected before anything is read."""
        files = [_file(f"f{i}.md", b"x") for i in range(MAX_BATCH_FILES + 1)]

        r = await _post(client, auth_headers, files, space="bl1")

        assert r.status_code == 413, r.text

    async def test_a_file_over_the_per_file_cap_is_refused(self, client, auth_headers):
        big = b"x" * (MAX_UPLOAD_BYTES + 1024)

        r = await _post(client, auth_headers, [_file("huge.md", big)], space="bl2")

        assert r.status_code == 200, "an oversized file is a per-file failure, not a bad request"
        result = r.json()["files"][0]
        assert result["stored"] is False
        assert "limit" in result["error"].lower()

    async def test_one_oversized_file_does_not_stop_the_others(self, client, auth_headers):
        big = b"x" * (MAX_UPLOAD_BYTES + 1024)

        r = await _post(
            client,
            auth_headers,
            [_file("fine.md"), _file("huge.md", big), _file("also-fine.md")],
            space="bl3",
        )

        body = r.json()
        assert body["files_succeeded"] == 2
        assert body["files_failed"] == 1

    async def test_the_batch_stays_under_its_aggregate_cap(self, client, auth_headers):
        """
        Each file is under the per-file cap; together they pass the batch cap.
        This is the case a per-file limit alone does not catch.
        """
        one_file = MAX_UPLOAD_BYTES - 1024
        count = (MAX_BATCH_BYTES // one_file) + 2
        files = [_file(f"big{i}.md", b"x" * one_file) for i in range(count)]

        r = await _post(client, auth_headers, files, space="bl4")

        assert r.status_code == 200, r.text
        body = r.json()
        assert body["files_failed"] > 0, "the aggregate cap should have stopped the tail"
        assert any("total limit" in (x["error"] or "").lower() for x in body["files"]), [
            x["error"] for x in body["files"]
        ]


class TestAuthentication:
    async def test_a_batch_upload_requires_a_token(self, client):
        r = await client.post(
            "/api/ingest/files",
            files=[_file("x.md")],
            data={"space": "ba1"},
        )
        assert r.status_code in (401, 403), r.text


class TestTheSingleFileEndpointIsUnchanged:
    """The batch endpoint is additive; the existing one is what callers use."""

    async def test_single_upload_still_works(self, client, auth_headers):
        r = await client.post(
            "/api/ingest/file",
            files={"file": ("solo.md", b"# Solo\n\nAlice shipped the release.", "text/markdown")},
            data={"space": "bu1"},
            headers=auth_headers,
        )

        assert r.status_code == 200, r.text
        assert r.json()["chunks_created"] >= 1

    async def test_single_upload_still_rejects_a_bad_filename(self, client, auth_headers):
        r = await client.post(
            "/api/ingest/file",
            files={"file": ("../evil.md", b"x", "text/markdown")},
            data={"space": "bu2"},
            headers=auth_headers,
        )
        assert r.status_code == 400, r.text
