-- Memory Vault — Knowledge Graph (M7)
-- Aligns pre-scaffolded `entities` and `relationships` tables with the
-- locked M7 schema, and adds the `entity_mentions` table.
--
-- Safe on empty tables: `entities` and `relationships` were scaffolded
-- in 001 but never populated (ingestion had no extraction before M7).

-- ---------------------------------------------------------------------------
-- entities: add per-space scoping
-- ---------------------------------------------------------------------------

ALTER TABLE entities
    ADD COLUMN space_id INTEGER NOT NULL REFERENCES memory_spaces(id) ON DELETE CASCADE;

DROP INDEX IF EXISTS entities_name_type_idx;

-- Per-space case-insensitive exact-match dedup (locked M7 rule).
CREATE UNIQUE INDEX entities_name_type_space_idx
    ON entities (lower(name), type, space_id);

CREATE INDEX entities_space_idx ON entities (space_id);
CREATE INDEX entities_type_idx ON entities (type);

-- ---------------------------------------------------------------------------
-- relationships: rename columns to match M7 plan; add chunk provenance
-- ---------------------------------------------------------------------------

ALTER TABLE relationships RENAME COLUMN from_entity_id TO source_entity_id;
ALTER TABLE relationships RENAME COLUMN to_entity_id   TO target_entity_id;
ALTER TABLE relationships RENAME COLUMN rel_type       TO type;

-- Chunk provenance — nullable because future manual/LLM tagging may not
-- be tied to a specific chunk. M7 extraction always populates it.
ALTER TABLE relationships
    ADD COLUMN chunk_id UUID REFERENCES chunks(id) ON DELETE CASCADE;

-- Rename old indexes to match new column names (drop + recreate).
DROP INDEX IF EXISTS relationships_from_idx;
DROP INDEX IF EXISTS relationships_to_idx;

CREATE INDEX relationships_source_idx ON relationships (source_entity_id);
CREATE INDEX relationships_target_idx ON relationships (target_entity_id);
CREATE INDEX relationships_chunk_idx  ON relationships (chunk_id);
CREATE INDEX relationships_type_idx   ON relationships (type);

-- ---------------------------------------------------------------------------
-- entity_mentions: one row per (entity, chunk, location)
-- This is what "M7 extracts mentions" refers to — every entity hit in a
-- chunk produces a row here, with character offsets preserved.
-- ---------------------------------------------------------------------------

CREATE TABLE entity_mentions (
    id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    entity_id    UUID NOT NULL REFERENCES entities(id) ON DELETE CASCADE,
    chunk_id     UUID NOT NULL REFERENCES chunks(id)   ON DELETE CASCADE,
    start_offset INTEGER NOT NULL,
    end_offset   INTEGER NOT NULL,
    created_at   TIMESTAMPTZ DEFAULT now()
);

CREATE INDEX entity_mentions_entity_idx ON entity_mentions (entity_id);
CREATE INDEX entity_mentions_chunk_idx  ON entity_mentions (chunk_id);
