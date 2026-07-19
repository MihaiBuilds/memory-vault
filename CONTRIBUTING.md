# Contributing to Memory Vault

Thanks for your interest in Memory Vault. This is a single-maintainer open-source project, so contributions are welcome but reviewed when time allows. Please read this guide before opening an issue or PR — it saves both of us time.

By participating in this project you agree to abide by the [Code of Conduct](CODE_OF_CONDUCT.md).

---

## Table of Contents

- [Reporting Bugs](#reporting-bugs)
- [Suggesting Features](#suggesting-features)
- [Reporting Security Vulnerabilities](#reporting-security-vulnerabilities)
- [Asking Questions](#asking-questions)
- [Setting Up a Dev Environment](#setting-up-a-dev-environment)
- [Coding Conventions](#coding-conventions)
- [Submitting a Pull Request](#submitting-a-pull-request)
- [What Gets Merged](#what-gets-merged)

---

## Reporting Bugs

Use the [bug report template](.github/ISSUE_TEMPLATE/bug_report.yml) — it walks you through the information that's actually useful.

**Before opening an issue, run the diagnostic bundler:**

```bash
docker compose exec app memory-vault diagnose
```

(Or `memory-vault diagnose` directly if you're running on the host.)

This produces a `memory-vault-diagnostic-YYYY-MM-DD-HHMMSS.zip` containing:

- Recent application logs (with bearer tokens, API keys, and DB passwords automatically redacted)
- Output of `memory-vault status`
- Platform / Python / environment info (sensitive env vars redacted)
- `docker compose ps` and recent DB logs (when run on the host)

**Review the zip before posting it.** Redaction is a safety net, not a guarantee — if you logged anything sensitive yourself, scrub it first.

Every API response includes an `X-Request-ID` header. If a bug happens during an HTTP request, quoting that ID in the issue helps me find the exact log lines fast.

## Suggesting Features

Use the [feature request template](.github/ISSUE_TEMPLATE/feature_request.yml). Two things make a feature request likely to land:

1. **Frame the problem first, then propose a solution.** "I can't do X because Y" beats "please add Z."
2. **Check the [Limitations section in the README](README.md#v10-limitations-honest).** If it overlaps with a documented v1.0 limitation, a +1 on the existing issue (or opening one) is more useful than a duplicate request.

Big features (new endpoints, new UI pages, new external integrations) should be discussed in an issue *before* any code is written. Drive-by PRs for big features will likely be closed politely.

## Reporting Security Vulnerabilities

**Do not open a public issue for security reports.** See [SECURITY.md](SECURITY.md) for the private disclosure process.

## Asking Questions

GitHub Issues is for bugs and concrete feature requests. For setup help, design questions, or "how do I…" — use [GitHub Discussions](https://github.com/MihaiBuilds/memory-vault/discussions). Questions opened as issues will be moved to Discussions.

---

## Setting Up a Dev Environment

Memory Vault runs in Docker for production, but local development uses a Python venv against a Dockerized Postgres.

### Prerequisites

- Python 3.11+
- Docker + Docker Compose
- Node 20+ (only if you're touching the dashboard)

### Backend setup

```bash
git clone https://github.com/MihaiBuilds/memory-vault.git
cd memory-vault

python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"

# Start Postgres only (the app runs locally for fast iteration)
docker compose up -d db

memory-vault migrate
memory-vault status   # sanity check

# Run the API in dev mode
uvicorn memory_vault.api.app:app --reload --port 8000
```

### Dashboard setup

```bash
cd web
npm install
npm run dev   # Vite dev server on :5173, proxies /api → localhost:8000
```

### Running tests

```bash
pytest                              # full suite
pytest tests/test_chat_api.py -v   # one file
pytest -k "redaction"              # by keyword
```

---

## Coding Conventions

Memory Vault is small enough that consistency matters more than rules. The general shape:

**Python**

- Python 3.11+, type hints on public functions
- Raw SQL via `psycopg`, no ORM
- `async`/`await` in the API layer; sync is fine for CLI and ingestion
- Errors at HTTP boundaries return structured JSON (see `src/memory_vault/api/app.py` exception handlers) — never leak stack traces
- Log identifiers (chunk IDs, space IDs, model names), never user content

**TypeScript / React**

- Functional components + hooks
- One page per file under `web/src/pages/`
- Tailwind for styling, no separate CSS files
- API calls go through the typed client in `web/src/api.ts`

**Commits**

- Imperative mood ("add chat router", not "added chat router")
- One logical change per commit when reasonable

**What to avoid**

- Adding new dependencies without justification — the dependency tree is deliberately small
- Speculative abstractions or "future flexibility" — match the existing direct style
- Comments that restate the code; comments explaining *why* are welcome
- Logging user-supplied content (queries, memory text) — only IDs and metadata

---

## Submitting a Pull Request

1. **Open an issue first** for anything beyond a small fix. Saves wasted work if the direction isn't right.
2. **Fork → branch off `main`** with a descriptive name (`fix/chat-stop-button`, `feat/pdf-adapter`).
3. **Keep PRs focused.** One concern per PR. Refactoring + feature in the same PR usually gets split.
4. **Run tests + lint locally** before pushing:
   ```bash
   pytest
   ruff check .
   ```
5. **Update docs** when you change user-facing behavior — README, docstrings, or the FAQ.
6. **Fill in the PR template** — it asks for the things I'd otherwise have to ask for in review.

PRs that don't pass CI will not be reviewed until they do. PRs that grow the dependency footprint significantly will get pushback.

## What Gets Merged

**Likely merged:**

- Bug fixes with a test that fails before and passes after
- Documentation improvements (typos, clarifications, missing examples)
- Performance improvements with measurements
- Adapters for new ingestion sources (matching the existing adapter pattern)
- Small UX improvements in the dashboard

**Likely deferred or declined:**

- Features that belong in the PRO tier (multi-user, encrypted backups, conflict resolution, etc.) — these have a planned home
- New external service integrations that add ongoing maintenance burden
- Large refactors of working code without a concrete payoff
- Style-only changes that fight the existing conventions

If you're unsure whether something fits, open an issue and ask before writing the code.

---

Thanks for reading this far. If you ship something to Memory Vault, you'll be credited in the release notes and the README's Credits section.

— Mihai
