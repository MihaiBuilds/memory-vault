-- Memory Vault — confine the remember-dedup index to stored memories
--
-- Migration 004 added a unique index on (space_id, content_hash) to stop two
-- concurrent `remember` calls storing the same memory twice. At the time only
-- `remember` persisted a content hash, so "one content hash per space" and
-- "one stored memory per space" meant the same thing.
--
-- File ingestion now persists a content hash too, so that a retry can
-- recognise chunks it already wrote. That makes the old index wrong for
-- ingested rows in two ways. A single document may legitimately repeat a
-- passage -- a line restated in a changelog, a phrase said twice in a
-- transcript -- and two unrelated documents may share a paragraph. Under the
-- 004 index the second occurrence is rejected, so ingesting an ordinary file
-- fails outright.
--
-- The fix is to say what 004 meant: the constraint belongs to memories stored
-- through `remember`, which never carry a source file. Chunks from ingestion
-- are covered instead by the identity index in migration 005, which includes
-- the source file and chunk position and so distinguishes a repeated passage
-- from a repeated write.

DROP INDEX IF EXISTS chunks_space_content_hash_idx;

CREATE UNIQUE INDEX chunks_space_content_hash_idx
    ON chunks (space_id, (metadata->>'content_hash'))
    WHERE metadata->>'content_hash' IS NOT NULL
      AND metadata->>'source_file' IS NULL;
