-- Memory Vault — enforce exact-duplicate detection in the database
--
-- The MCP `remember` tool checked for an existing content hash and then
-- inserted in a separate statement. Two concurrent calls could both pass the
-- check before either insert ran, so both stored the same memory. No database
-- constraint backed the guarantee the tool advertised.
--
-- Scope: this index covers rows that carry metadata->>'content_hash', which
-- today means rows written by MCP `remember`. File ingestion and ingest_text
-- do not persist a content hash, so their rows have NULL here and are not
-- constrained -- NULLs are never equal in a unique index, so they would be
-- exempt whether or not the WHERE clause is present. The predicate makes that
-- scope explicit rather than implied, and keeps the index off rows it can
-- never apply to. Widening the invariant to the ingestion paths means
-- persisting the hash there first; that is deliberately not done here.

-- Collapse any duplicates already committed by the pre-fix race, keeping the
-- oldest row of each group. Without this, CREATE UNIQUE INDEX fails on a
-- database that hit the bug and the container will not finish booting.
WITH ranked AS (
    SELECT id,
           row_number() OVER (
               PARTITION BY space_id, metadata->>'content_hash'
               ORDER BY created_at, id
           ) AS rn
    FROM chunks
    WHERE metadata->>'content_hash' IS NOT NULL
)
DELETE FROM chunks
WHERE id IN (SELECT id FROM ranked WHERE rn > 1);

CREATE UNIQUE INDEX chunks_space_content_hash_idx
    ON chunks (space_id, (metadata->>'content_hash'))
    WHERE metadata->>'content_hash' IS NOT NULL;
