-- Memory Vault — optional expiry for API tokens
--
-- Until now a token was valid from creation until someone revoked it by hand.
-- That is fine for the token on your own laptop and wrong for the one you
-- pasted into a CI job or handed to a script six months ago: nothing expires
-- it, and nothing reminds you it exists.
--
-- `expires_at` is nullable and defaults to NULL, which means "never expires".
-- Every token that already exists keeps working exactly as before, and a
-- caller who does not ask for an expiry still gets a permanent token. The
-- column only does something when someone opts in.
--
-- Enforcement lives in the auth path rather than in a constraint, because an
-- expired token is not invalid data -- it is a row that should stop granting
-- access while remaining visible to `token list` so the operator can see what
-- lapsed and when.

ALTER TABLE api_tokens
    ADD COLUMN IF NOT EXISTS expires_at TIMESTAMPTZ;

-- The auth path filters on `revoked_at IS NULL` and then checks expiry, so the
-- useful index is over live tokens only. Partial keeps it small: expired and
-- revoked rows are kept for the audit trail but never looked up by this path.
CREATE INDEX IF NOT EXISTS api_tokens_active_expiry_idx
    ON api_tokens (expires_at)
    WHERE revoked_at IS NULL;
