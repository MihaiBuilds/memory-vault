# Security Policy

Memory Vault is a self-hosted memory database. Vulnerabilities reported responsibly will be acknowledged, fixed, and credited.

## Reporting a Vulnerability

**Please do not open a public GitHub issue for security reports.** Public disclosure before a fix is available puts every Memory Vault user at risk.

Instead, email **support@mihaibuilds.com** with the subject line:

```
Security: <one-line summary>
```

Include:

- A description of the vulnerability and its impact
- Steps to reproduce (or a proof-of-concept)
- Affected version(s) — output of `memory-vault status` or the Docker image tag
- Your contact info if you'd like credit when the fix lands

Encrypted reports are welcome — request a PGP key in your first email if you want one.

## Response

- **Acknowledgement:** within 7 days
- **Initial assessment:** within 14 days (severity, affected versions, fix plan)
- **Fix + disclosure:** coordinated. Patch lands first; a public advisory with credit follows after users have had a reasonable window to update.

If a report goes 14 days without an acknowledgement, escalate by opening a public issue with the words "security follow-up — no response on private channel" — but do **not** include vulnerability details in that public issue.

## Supported Versions

Memory Vault is a single-maintainer project. Only the **latest minor release** in the current major series receives security fixes. Older minors will not be backported.

| Version | Supported       |
| ------- | --------------- |
| 1.x     | ✅ Latest minor  |
| < 1.0   | ❌ Pre-release   |

When v2.0 ships, v1.x will receive security fixes for at least 90 days after the v2.0 release.

## Disclosure Policy

Memory Vault follows **coordinated disclosure**:

1. Fix is developed and tested privately.
2. Patch is released as a tagged version (e.g. `v1.0.1`).
3. A GitHub Security Advisory is published, crediting the reporter (unless anonymity was requested).
4. Users are encouraged to update via `docker compose pull && docker compose up -d`.

Reporters are credited by name and link unless they ask not to be. Bounties are not offered (single-maintainer project, no budget) — the credit and the fix are the reward.

## Out of Scope

- Vulnerabilities in dependencies that have not yet been published as advisories. Please report those upstream first.
- Self-inflicted misconfiguration (e.g. running with `API_AUTH_ENABLED=false` exposed to the public internet — this is documented as local-dev-only).
- Social engineering, denial-of-service via raw resource exhaustion (Memory Vault is designed for self-hosted single-tenant use).
- Issues that require physical or admin access to the host machine.

## Threat Model (v1.0)

Memory Vault is a **single-tenant, self-hosted** memory database. The threat model reflects that posture — it is not an internet-exposed multi-user SaaS, and the security boundary stops at the host's network.

### Who's the user?

A developer or hobbyist running Memory Vault on their own machine, homelab, or single-purpose VPS. They control the host, the network, and who has access to the bearer tokens. They're security-aware enough to put it behind a reverse proxy with TLS if exposed beyond localhost.

### Data sensitivity

Memory contents are personal notes, conversation history, and project context — sensitive to the user but not regulated data (no PII subject access requests, no PHI, no payment data). Bearer tokens are the only secret material handled by the application; database credentials are configured via environment, not stored.

### In-scope attacks

| Attack | Defense |
|---|---|
| Network MITM between client and API | TLS is the operator's responsibility (reverse proxy in production); bearer tokens are useless without the matching hash in the DB |
| Stolen bearer token | Rotate via `memory-vault token revoke <prefix>`; `last_used_at` column lets the operator audit suspicious tokens |
| Brute-force token guessing | Rate limiter (120 req/min per IP, configurable); tokens are 32 random bytes from `secrets.token_urlsafe` (~256 bits of entropy); hash lookup uses `hmac.compare_digest` for constant-time comparison |
| SQL injection via search queries, space names, file uploads | All raw SQL uses `%s` parameterization (no f-string substitution of user values); Pydantic validates every input at the API boundary; space names match a strict regex (`^[a-z0-9][a-z0-9-]*$`) |
| XSS in dashboard | React's default escaping is in effect; no `dangerouslySetInnerHTML` anywhere; no `eval` or `new Function` |
| DoS via oversized inputs | Search queries capped at 8 KB; chat messages capped at 32 KB; ingested text capped at 1 MB; file uploads capped at 25 MB; rate limiter trips at 120 req/min |
| Stack-trace leakage in error responses | Global `Exception` handler returns generic 500 with no traceback; `psycopg.OperationalError` returns generic 503; full traces go to logs only, correlated by `X-Request-ID` |
| Credentials leaking via the diagnostic bundle | `memory-vault diagnose` redacts bearer tokens, `mv_*` tokens, password/secret/api_key kv pairs, and known sensitive env vars before producing the zip |

### Out of scope (acknowledged)

| Threat | Why deferred |
|---|---|
| Multi-tenant isolation | Memory Vault v1.0 is single-user. PRO (M9) introduces multi-user with `owner_id` enforcement |
| Encryption at rest | Operator's responsibility — use full-disk encryption on the host, or a managed Postgres with TDE |
| External penetration audit | Single-maintainer pre-revenue product; revisit post-launch when there's budget |
| Key Management Service | Bearer tokens are random 32-byte secrets stored as SHA-256 hashes — no rotation infra needed at this scale |
| Malicious LLM output rendered in dashboard | Chat answers are plain text rendered by React; no HTML/JS execution path. If a future feature renders LLM output as Markdown, it will need an explicit XSS audit |
| Compromised host machine | Out of scope by design — the host's OS/admin is the trust boundary |

## Security Test Matrix

The repository ships with a re-runnable curl-based pentest at [`scripts/security-pentest.sh`](scripts/security-pentest.sh). It covers:

- **Auth** — missing/wrong-scheme/invalid-token rejection, valid-token success, `/api/health` unauthenticated access
- **Input validation** — malformed JSON, missing fields, empty queries, oversized payloads (search/ingest), invalid space names
- **Injection patterns** — SQL injection in search query, Unicode RTL-override in space name, path traversal in upload filename
- **Rate limit** — manual verification (fire ~140 requests in 60s, observe a 429 with `Retry-After`)

Run before tagging a release:

```bash
docker compose up -d
TOKEN=$(memory-vault token create pentest)
API_URL=http://localhost:8000 API_TOKEN="$TOKEN" bash scripts/security-pentest.sh
memory-vault token revoke "${TOKEN:0:11}"
```

Every case must pass before a tag goes out.

## Static Analysis & Dependency Health

Public-tier security tooling enabled in CI:

- **Bandit** (Python) — runs locally before each release; `# nosec` annotations are in-source with justifications. Findings: zero medium/high.
- **CodeQL** ([.github/workflows/codeql.yml](.github/workflows/codeql.yml)) — security-extended query pack, scans Python and TypeScript on push, PR, and weekly cron.
- **Dependabot** ([.github/dependabot.yml](.github/dependabot.yml)) — weekly checks on Python, npm, GitHub Actions, and Docker base images. Minor and patch updates grouped to reduce PR noise.
- **npm audit** — run before each release; production+dev dependencies kept at zero advisories.
