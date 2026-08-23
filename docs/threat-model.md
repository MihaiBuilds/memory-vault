# Threat model

This document states what Memory Vault protects, what it assumes about its
environment, and — importantly — what it does **not** defend against. It exists
so you can decide whether the default deployment fits your situation, and what
to change if it doesn't.

For reporting a vulnerability, supported versions, and disclosure policy, see
[SECURITY.md](../SECURITY.md).

You do not have to take the claims below on trust. The repository ships a
re-runnable pentest script that exercises the auth, input-validation and
injection defenses against a live instance — see
[Verifying the posture](#verifying-the-posture).

Memory Vault is a **single-tenant, self-hosted** application. That assumption
runs through every decision below. If your deployment breaks that assumption,
read [Deployments beyond the default model](#deployments-beyond-the-default-model).

---

## Who this is for

A developer or hobbyist running Memory Vault on their own machine, homelab, or
single-purpose VPS, where they control the host, the network, and who holds the
bearer tokens. The security boundary stops at the host's network.

If that describes your deployment, the defaults are built for you. If it does
not, the gap is documented rather than assumed away.

Memory contents are personal notes, conversation history, and project context —
sensitive to the operator, but not regulated data. Memory Vault is not built for
PHI, payment data, or anything carrying subject-access obligations.

---

## What you are protecting

The data in a Memory Vault instance is usually more sensitive than the sum of
its parts. Individually a note is a note; collectively, an instance is a
searchable index of what someone has been thinking about, working on, and
deciding, often over months.

| Asset | Where it lives | Why it matters |
| --- | --- | --- |
| Memory content | `chunks.content` in Postgres | The notes, documents and conversations themselves |
| Embeddings | `chunks.embedding` (pgvector) | Semantic content is partially recoverable from embeddings |
| Knowledge graph | `entities`, `entity_mentions`, `relationships` | Names, tools, projects and how they connect — a social/work graph |
| Query history | `query_log` | What was searched for and when, which can be as revealing as the content |
| API tokens | `api_tokens.token_hash` | SHA-256 hashes; the plaintext is shown once at creation and never stored |
| Database credentials | Environment variables | Full read/write access to everything above |

---

## Trust boundaries

There are four, and only the first two are enforced by Memory Vault itself.

**1 · Network → REST API.** Every route requires a bearer token except
`GET /api/health`, which is unauthenticated so container orchestrators can probe
it without credentials. It returns status, version and the embedding model name —
no memory content.

**2 · REST API → database.** The application holds one Postgres credential and
uses it for everything.

**3 · Host → container.** The images run as a non-root user (uid 10001) and the
shipped compose file gives them a read-only root filesystem, drops all Linux
capabilities, and sets `no-new-privileges`. Nothing is written inside the
container at runtime: the embedding model is baked in at build time, logs go to
a mounted volume, and streamed uploads go to a tmpfs. This narrows what a
process that escapes the application can do; it does not contain an attacker who
has the host.

**4 · Memory Vault → LLM endpoint.** Outbound only, to a URL the operator
supplies. Memory Vault does not restrict where that points; see
[Outbound requests to the LLM](#outbound-requests-to-the-llm).

---

## In scope — what Memory Vault defends against

| Attack | Defense |
| --- | --- |
| Unauthenticated access to memory | Bearer token required on every route except `/api/health`. Tokens are 32 random bytes from `secrets.token_urlsafe`, stored only as SHA-256 hashes |
| Brute-force token guessing | Token entropy plus a rate limiter (120 req/min per IP, configurable); hash comparison uses `hmac.compare_digest` for constant-time behaviour |
| Stolen bearer token | Revoke with `memory-vault token revoke <prefix>`; revocation takes effect on the next request. `last_used_at` lets the operator audit suspicious tokens |
| SQL injection | All raw SQL uses `%s` parameter binding — no f-string substitution of user values. Pydantic validates every input at the API boundary; space names must match `^[a-z0-9][a-z0-9-]*$` |
| Path traversal in static file serving | `_safe_static_path()` sanitises with `os.path.commonpath` and `os.path.realpath`, and rejects malformed input before composition |
| XSS in the dashboard | React's default escaping; no `dangerouslySetInnerHTML`, no `eval`, no `new Function` anywhere in the web source |
| Denial of service via oversized inputs | Search queries capped at 8,000 characters; chat messages at 32,000; ingested text at 1,000,000; file uploads stream to disk and abort at 25 MB |
| Stack-trace leakage in error responses | A global exception handler returns a generic 500 with no traceback; `psycopg.OperationalError` returns a generic 503. Full traces go to logs only, correlated by `X-Request-ID` |
| Credentials leaking via the diagnostic bundle | `memory-vault diagnose` redacts bearer tokens, `mv_*` values, password/secret/api_key pairs, and known sensitive environment variables before producing the zip |

---

## Out of scope — what Memory Vault does NOT defend against

Each of these is a deliberate boundary, not an oversight. Knowing where they sit
is what lets you decide whether the default deployment fits your situation.

**A stolen API token.** Tokens carry full access to every space in the instance.
There are no scopes and no per-space permissions. Treat a token as equivalent to
the whole database.

**Anyone with database access.** Memory content and embeddings are stored in
plaintext. There is no application-level encryption. Someone who can read the
Postgres volume can read everything. Use full-disk encryption on the host, or a
managed Postgres with encryption at rest.

**A malicious operator or a compromised host.** Out of scope by design — the
host's OS and administrator are the trust boundary.

**Multiple users on one instance.** There is no user model. Spaces are an
organisational convenience, **not a security boundary** — any valid token reads
and writes every space. Do not use spaces to separate different people's data.

**Content-based attacks on the LLM.** Text stored in memory is retrieved and
placed into LLM prompts. Memory Vault does not detect or neutralise prompt
injection. If you ingest untrusted content, that content can influence what the
model does when it is later recalled.

**Malicious LLM output rendered in the dashboard.** Chat answers are plain text
rendered by React, with no HTML or JS execution path. If a future feature renders
LLM output as Markdown, that will need an explicit XSS review.

**Traffic interception.** The API speaks plain HTTP. There is no built-in TLS;
terminate it at a reverse proxy.

**Distributed denial of service.** The rate limiter is per-IP and in-memory. It
resets on restart and is trivially bypassed by a distributed source.

**Deletion recovering storage.** `forget` is a soft delete — the row is marked,
not removed, so the content remains in the database. Tracked in issue #74.

---

## Outbound requests to the LLM

The chat endpoints accept an `llm_url` and send requests to it. **This is a
product feature, not an oversight**: it is what lets you point Memory Vault at
your own LM Studio, Ollama, or any OpenAI-compatible endpoint. Memory Vault
deliberately does not allow-list that URL, because doing so would break the
configurability that makes it useful.

The consequence is honest: on a deployment where an attacker can reach the API
with a valid token, that request can be aimed at any address the container can
reach — including link-local metadata endpoints such as `169.254.169.254` on
cloud instances.

Under the single-tenant model this grants no access the caller did not already
have, because anyone holding a valid token is already the operator. That
reasoning holds exactly as long as the token does: **a leaked token turns this
into a request-forgery primitive against whatever the container can reach.**
That is the real risk, and it is why the token hygiene and egress guidance below
are not optional on an exposed host. **The correct mitigation is at the network
layer, not in the application.**

Static analysis flags this pattern (CodeQL `py/partial-ssrf`). Those alerts are
dismissed as "won't fix" with this rationale rather than being suppressed
silently — the behaviour is intended and documented here.

Separately, CodeQL reports `py/path-injection` against the static-file handler.
Those are dismissed as false positives on different grounds: the sanitiser
described in the in-scope table above is CodeQL's own recommended pattern. That
one is a defended path, not an accepted risk.

---

## Deployments beyond the default model

The default model assumes the operator is the only user, on their own machine,
LAN, or single-tenant VM. If you are running on a publicly reachable host, a
shared network, or a segment with sensitive internal services, apply the
following.

### API token hygiene

- Issue one token per client rather than sharing a single token.
- Store tokens in a secret manager or an environment file with restricted
  permissions — never in shell history or a committed `.env`.
- Rotate on a schedule you actually keep, and immediately on suspected exposure:
  `memory-vault token create <name>`, update the client, then
  `memory-vault token revoke <prefix>`.
- `memory-vault token list` shows `last_used_at` — use it to find tokens that can
  be revoked.

### Network scoping

- Bind to `127.0.0.1` and reach it through a reverse proxy, rather than exposing
  `0.0.0.0:8000` directly. In `docker-compose.yml`, publish as
  `"127.0.0.1:8000:8000"`.
- The default compose file also publishes Postgres on `5432`. On any host that is
  not your own workstation, remove that port mapping — the application reaches
  the database over the compose network and does not need it.
- Restrict `API_CORS_ORIGINS` to the origins you actually serve. The default is
  `*`, which suits local use and is too permissive for a public host.

### Outbound network policy

For deployments where the API is publicly reachable, block egress to link-local
and internal ranges at the firewall or container network layer — in particular
`169.254.169.254` on cloud providers. This is the correct mitigation for the
`llm_url` behaviour described above.

### TLS termination

Put a reverse proxy (nginx, Caddy, Traefik) in front and terminate TLS there.
Bearer tokens travel in the `Authorization` header; over plain HTTP on an
untrusted network they are readable in transit.

### Rate limiting

The built-in limiter is per-process and in-memory, sized for a single-tenant box.
On a public deployment, rate-limit at the proxy layer instead, where it survives
restarts and sees all workers.

### Database credentials

Change the default `memory_vault` / `memory_vault` credentials in
`docker-compose.yml`. They are convenient defaults for local use and unsuitable
anywhere else.

### Least-privilege database roles

By default one database user both creates the schema at start-up and serves
every request, so anything reaching that connection has rights to change or drop
your data. Migrations define three group roles you can adopt to narrow that:

| Role | Rights |
| --- | --- |
| `memory_vault_app` | `SELECT`/`INSERT`/`UPDATE`/`DELETE`. No schema changes |
| `memory_vault_readonly` | `SELECT` only — for dashboards, backups, and ad-hoc psql |
| `memory_vault_migrator` | Schema changes, used only while migrations run |

They are group roles with no login of their own, so nothing changes until you
opt in. To adopt the split, create two login roles and grant each one group:

```sql
CREATE ROLE memory_vault_app_login LOGIN PASSWORD 'change_me';
GRANT memory_vault_app TO memory_vault_app_login;

CREATE ROLE memory_vault_migrate LOGIN PASSWORD 'change_me_too';
GRANT memory_vault_migrator TO memory_vault_migrate;
```

Then point the application at the app role and give it the migrator credentials
for migrations only:

```yaml
DB_USER: memory_vault_app_login
DB_PASSWORD: change_me
DB_MIGRATION_USER: memory_vault_migrate
DB_MIGRATION_PASSWORD: change_me_too
```

With `DB_MIGRATION_USER` unset, migrations run as `DB_USER` — the single-role
setup, unchanged.

One caveat: after adopting the split, run future migrations as the migrator
role. Tables it creates carry default privileges for the app and read-only
roles; tables created by some other role do not.

---

## Verifying the posture

The repository ships a re-runnable curl-based pentest at
[`scripts/security-pentest.sh`](../scripts/security-pentest.sh), covering auth
rejection paths, input validation, injection patterns (SQL, Unicode RTL-override
in space names, path traversal in upload filenames), and rate limiting.

```bash
docker compose up -d
TOKEN=$(memory-vault token create pentest)
API_URL=http://localhost:8000 API_TOKEN="$TOKEN" bash scripts/security-pentest.sh
memory-vault token revoke "${TOKEN:0:11}"
```

Static analysis and dependency health in CI:

- **CodeQL** ([.github/workflows/codeql.yml](../.github/workflows/codeql.yml)) —
  security-extended query pack, Python and TypeScript, on push, PR, and weekly.
- **Dependabot** ([.github/dependabot.yml](../.github/dependabot.yml)) — weekly
  checks on Python, npm, GitHub Actions, and Docker base images.
- **Bandit** and **npm audit** — run before each release.

---

## Known gaps

Recorded here rather than left implicit. These are accepted, not hidden.

| Gap | Status |
| --- | --- |
| Containers run as root | Closed — non-root (uid 10001), read-only rootfs, all capabilities dropped |
| One database role for both migrations and runtime | Still the default; opt-in roles available (see above) |
| API tokens never expire | Open |
| No per-space or per-scope token permissions | Not planned — spaces are not a security boundary |
| Memory content stored unencrypted | Not planned — disk encryption is the operator's responsibility |
| `forget` does not reclaim storage | Tracked in issue #74 |
| No TLS in the application | Not planned — terminate at a proxy |
| No external penetration audit | Single-maintainer project; revisit when there is budget |

This table is updated as gaps close; check the version you are running.
