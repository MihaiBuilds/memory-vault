-- Memory Vault — make file ingestion idempotent on retry
--
-- Each chunk of a file was inserted and committed on its own. A failure
-- partway through left the earlier chunks committed, and retrying the file
-- appended a second copy of every chunk that had already landed.
--
-- Two things fix that. The ingestion loop now runs inside one transaction, so
-- a failed file commits nothing. This index is the durable half: it gives a
-- chunk an identity, so re-inserting the same chunk of the same file is a
-- no-op rather than a duplicate, no matter which code path does the writing.
--
-- The key is (space, source file, chunk index, content hash) rather than the
-- content hash alone. Hashing content by itself would treat two legitimately
-- identical passages in one document -- a repeated line in a changelog, the
-- same phrase in two transcript turns -- as duplicates and silently drop the
-- second. Including the chunk index keeps those distinct while still
-- collapsing a genuine retry, which re-parses the file and produces the same
-- chunk at the same index. The hash stays in the key so that editing a file
-- and re-ingesting it replaces nothing silently: changed content is a
-- different chunk identity and inserts normally.
--
-- Scope: rows that carry both markers in metadata, which means rows written by
-- file ingestion from here on. Rows already stored have no content hash and
-- are exempt; they were never at risk from a future retry, and rewriting every
-- existing row to backfill a hash would mean a full table rewrite on upgrade
-- for no correctness gain.

CREATE UNIQUE INDEX chunks_ingest_identity_idx
    ON chunks (
        space_id,
        (metadata->>'source_file'),
        chunk_index,
        (metadata->>'content_hash')
    )
    WHERE metadata->>'content_hash' IS NOT NULL
      AND metadata->>'source_file' IS NOT NULL;
