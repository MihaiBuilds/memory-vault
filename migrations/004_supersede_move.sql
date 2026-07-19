-- Memory Vault — Supersede / Move support
-- Adds is_superseded + superseded_by columns to chunks for the supersede_memory tool.
-- The is_superseded column replaces the JSONB-based forgotten pattern for supersession.

ALTER TABLE chunks
    ADD COLUMN IF NOT EXISTS is_superseded BOOLEAN NOT NULL DEFAULT false,
    ADD COLUMN IF NOT EXISTS superseded_by UUID REFERENCES chunks(id) ON DELETE SET NULL;

CREATE INDEX IF NOT EXISTS chunks_superseded_idx ON chunks (is_superseded);
