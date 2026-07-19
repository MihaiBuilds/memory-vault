-- Memory Vault — API tokens
-- Bearer token authentication for the REST API.
-- Tokens are stored as SHA-256 hashes; the plaintext is shown once at creation.

CREATE TABLE api_tokens (
    id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name         TEXT NOT NULL,
    token_hash   TEXT UNIQUE NOT NULL,
    token_prefix TEXT NOT NULL,              -- first 8 chars for identification
    created_at   TIMESTAMPTZ DEFAULT now(),
    last_used_at TIMESTAMPTZ,
    revoked_at   TIMESTAMPTZ
);

CREATE INDEX api_tokens_hash_idx ON api_tokens (token_hash);
