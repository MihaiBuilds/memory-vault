-- Memory Vault — Initial Schema
-- PostgreSQL 16 + pgvector

CREATE EXTENSION IF NOT EXISTS vector;

-- Memory spaces
CREATE TABLE memory_spaces (
    id          SERIAL PRIMARY KEY,
    name        TEXT UNIQUE NOT NULL,
    description TEXT,
    created_at  TIMESTAMPTZ DEFAULT now()
);

-- Chunks — the core memory unit
CREATE TABLE chunks (
    id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    space_id     INTEGER REFERENCES memory_spaces(id),
    content      TEXT NOT NULL,
    embedding    vector(384),
    content_tsv  tsvector,
    source       TEXT,                   -- where it came from (file path, URL, MCP tool)
    speaker      TEXT,                   -- 'human', 'assistant', or null
    metadata     JSONB DEFAULT '{}',
    importance   FLOAT DEFAULT 0.5,
    chunk_index  INTEGER NOT NULL DEFAULT 0,
    created_at   TIMESTAMPTZ DEFAULT now(),
    updated_at   TIMESTAMPTZ DEFAULT now()
);

-- Indexes
CREATE INDEX chunks_embedding_idx ON chunks
    USING hnsw (embedding vector_cosine_ops);
CREATE INDEX chunks_content_tsv_idx ON chunks
    USING gin (content_tsv);
CREATE INDEX chunks_space_idx ON chunks (space_id);
CREATE INDEX chunks_created_idx ON chunks (created_at);
CREATE INDEX chunks_metadata_idx ON chunks
    USING gin (metadata jsonb_path_ops);

-- Auto-update tsvector on insert/update
CREATE OR REPLACE FUNCTION chunks_tsv_trigger() RETURNS trigger AS $$
BEGIN
    NEW.content_tsv := to_tsvector('english', COALESCE(NEW.content, ''));
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER chunks_tsv_update
    BEFORE INSERT OR UPDATE OF content ON chunks
    FOR EACH ROW EXECUTE FUNCTION chunks_tsv_trigger();

-- Knowledge graph: entities
CREATE TABLE entities (
    id         UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name       TEXT NOT NULL,
    type       TEXT NOT NULL,            -- person, project, tool, concept
    properties JSONB DEFAULT '{}',
    created_at TIMESTAMPTZ DEFAULT now(),
    updated_at TIMESTAMPTZ DEFAULT now()
);

CREATE UNIQUE INDEX entities_name_type_idx ON entities (lower(name), type);

-- Knowledge graph: relationships
CREATE TABLE relationships (
    id               UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    from_entity_id   UUID REFERENCES entities(id) ON DELETE CASCADE,
    to_entity_id     UUID REFERENCES entities(id) ON DELETE CASCADE,
    rel_type         TEXT NOT NULL,       -- works_on, uses, depends_on
    strength         FLOAT DEFAULT 1.0,
    supporting_chunks UUID[],
    created_at       TIMESTAMPTZ DEFAULT now()
);

CREATE INDEX relationships_from_idx ON relationships (from_entity_id);
CREATE INDEX relationships_to_idx ON relationships (to_entity_id);

-- Query log for observability
CREATE TABLE query_log (
    id             UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    query_text     TEXT,
    space_ids      INTEGER[],
    result_count   INTEGER,
    top_similarity FLOAT,
    latency_ms     INTEGER,
    created_at     TIMESTAMPTZ DEFAULT now()
);

-- Seed: default space
INSERT INTO memory_spaces (name, description)
VALUES ('default', 'Default memory space');
