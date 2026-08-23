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

## Threat Model

Memory Vault is a **single-tenant, self-hosted** memory database. The full threat model — assets, trust boundaries, what is defended, what is explicitly not defended, and hardening guidance for deployments beyond the default model — lives in **[docs/threat-model.md](docs/threat-model.md)**.

Read it before exposing an instance beyond your own machine.

## Static Analysis & Dependency Health

Public-tier security tooling enabled in CI:

- **Bandit** (Python) — runs locally before each release; `# nosec` annotations are in-source with justifications. Findings: zero medium/high.
- **CodeQL** ([.github/workflows/codeql.yml](.github/workflows/codeql.yml)) — security-extended query pack, scans Python and TypeScript on push, PR, and weekly cron.
- **Dependabot** ([.github/dependabot.yml](.github/dependabot.yml)) — weekly checks on Python, npm, GitHub Actions, and Docker base images. Minor and patch updates grouped to reduce PR noise.
- **npm audit** — run before each release; production+dev dependencies kept at zero advisories.
