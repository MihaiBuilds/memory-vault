-- Memory Vault — views for the live (non-forgotten) knowledge graph
--
-- Forgetting a memory is a soft delete: the chunk stays in the table with
-- `forgotten` set in its metadata. Every graph query therefore has to exclude
-- rows backed by a forgotten chunk, and until now each one restated that
-- predicate itself -- nine times across the graph endpoints, in two different
-- shapes. A query that forgot it leaked forgotten memories through the graph,
-- which is what #109 reported.
--
-- These views state the rule once. Queries that read from them cannot forget
-- it, and a new graph surface gets the filter by default rather than by the
-- author remembering to add it.
--
-- The two views are not symmetrical, and the difference is deliberate:
--
--   entity_mentions.chunk_id is NOT NULL, so a mention always has a backing
--   chunk and a plain join expresses "the chunk is not forgotten".
--
--   relationships.chunk_id is nullable. A relationship with no backing chunk
--   is not extracted from a memory -- it is the shape future manual or
--   LLM-assigned links take -- so it has no chunk that could be forgotten and
--   must stay visible. An inner join here would silently delete that whole
--   category from the graph, so the rule is written as "no forgotten chunk
--   backs this row" rather than "a live chunk backs this row".

CREATE VIEW live_entity_mentions AS
    SELECT em.*
    FROM entity_mentions em
    JOIN chunks c ON c.id = em.chunk_id
    WHERE (c.metadata->>'forgotten')::boolean IS NOT TRUE;

CREATE VIEW live_relationships AS
    SELECT r.*
    FROM relationships r
    WHERE NOT EXISTS (
        SELECT 1
        FROM chunks c
        WHERE c.id = r.chunk_id
          AND (c.metadata->>'forgotten')::boolean IS TRUE
    );
