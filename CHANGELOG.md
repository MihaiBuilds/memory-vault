# Changelog

All notable changes to Memory Vault are documented here.

The format follows [Keep a Changelog 1.1.0](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [1.1.0] — 2026-08-08

Housekeeping release: dependency upgrades + external contributor docs. No code
authored by MV — every change is either a merged upstream dependency bump or a
docs contribution. All 5 Python dependabot PRs merged one-by-one with a
throwaway-container smoke test between each; TS 7 major deferred (upstream
peer-dep blocker); web-deps group merged as-is.

### Changed

- **`python-multipart` requirement raised to `>=0.0.32`** (was `>=0.0.29`).
  Ships upstream file-upload parser speedups. Smoke: throwaway-stack file
  upload via `/api/ingest/file` succeeds end-to-end. ([#94])
- **`psycopg-pool` requirement raised to `>=3.3.1,<4`** (was `>=3.1,<4`).
  DB pool client bump. Smoke: 5 rapid-fire ingests + 5 searches exercise
  pool checkout/return cleanly, no errors in pool logs. ([#96])
- **`ruff` requirement raised to `>=0.16.1`** (was `>=0.15.20`). Pre-merge
  verification: `ruff check src/ tests/` + `ruff format --check` both clean
  on the current codebase with 0 new findings. ([#93])
- **`fastapi` requirement raised to `>=0.141.1,<1`** (was `>=0.136.1,<1`).
  5-minor jump; highest Python-side risk in the batch. Smoke: 12/12 endpoint
  sweep passed — health, spaces list+create, ingest text+file, search,
  chunks list+get, graph entities+visualize, `/docs`, 401 handling. ([#95])
- **`sentence-transformers` requirement raised to `>=5.6.1,<6`** (was
  `>=5.5.0,<6`). Upstream 5.6.1 patches a RoBERTa-family flash-attention
  quality bug (not relevant to MV — we use `all-MiniLM-L6-v2`). Semantic
  smoke: two-topic ranking test confirms embedding quality (database-topic
  query: 0.71 vs 0.06; food-topic query: 0.56 vs -0.06; correct ranking
  both directions). ([#91])
- **Web dev-deps refreshed as a group.** `@types/node` 26.1.1→26.1.2,
  `@types/react` 19.2.17→19.2.18, `@types/react-dom` 19.2.3→19.2.4,
  `@vitejs/plugin-react` 6.0.4→6.0.5, `eslint` 10.7.0→10.8.0, `globals`
  17.7.0→17.9.0, `typescript-eslint` 8.65.0→8.66.0, `vite` 8.1.5→8.2.0.
  Smoke: full docker image build succeeds (both Node/Vite bundle stage
  and Python image stage). `typescript` intentionally held at 6.x — see
  Deferred. ([#134])

### Documentation

- **README installation section restructured.** `uv sync` is now the
  recommended path with an explicit `pip + venv` fallback block for users
  who don't want `uv`. The missing `python -m spacy download en_core_web_sm`
  step is now documented in both paths (previously caused a cryptic startup
  failure on first no-Docker run). ([#136], based on [#47])
- **MCP setup section expanded.** Global-scope config for Claude Code
  (`~/.claude/.mcp.json` + `enabledMcpjsonServers`) is now documented
  alongside project scope. MCP `command` uses `.venv/bin/python` so the
  config works without needing the venv pre-activated. Docker `DB_HOST`
  guidance clarified (`127.0.0.1` vs `localhost`). ([#136], based on [#47])
- **MCP Troubleshooting subsection added.** Covers `ModuleNotFoundError`,
  spaCy `OSError [E050]`, `claude --debug mcp`, missing
  `enabledMcpjsonServers`, and the Docker `DB_HOST` refused-connection
  case. ([#136], based on [#47])
- **MCP Registry status badge added to README.** ([#135])

### Deferred

- **TypeScript 6.0.3 → 7.0.2** blocked by upstream peer-dep cap.
  `typescript-eslint@8.66.0` requires `typescript >=4.8.4 <6.1.0`, so TS 7
  is not installable alongside our current lint tooling. Dependabot will
  reopen the upgrade PR when `typescript-eslint@9.x` ships with TS 7
  support. No runtime impact (TS is a build-time tool, and TS 6 vs 7
  produces functionally-identical JS output for our code). ([#92])

### Contributors

- [@skorten] (Sean Korten) — [#47] (uv setup, global MCP scope,
  troubleshooting section — adapted and shipped in [#136])

## [1.0.10] — 2026-08-08

### Added

- **Automatic MCP Registry publishing on release tag.** Every `v*.*.*` tag now
  pushes the checked-in `server.json` to `registry.modelcontextprotocol.io`
  via GitHub OIDC — no stored secret required. Prior releases left the
  registry listing to drift; v1.0.10 is the first release where the registry,
  the ghcr image, and the git tag stay in lockstep automatically. ([#125])
- **MCP server import smoke test.** `tests/test_mcp_server_import.py` imports
  `memory_vault.mcp.server` and asserts the FastMCP instance + the four
  canonical tools exist. Two lines of test that would have failed CI on
  v1.0.9 and blocked the bad release. ([#127])

### Changed

- **`mcp` dependency pinned to `>=1.28.1,<2.0.0`.** mcp 2.0.0 (released
  2026-07-28) removed `mcp.server.fastmcp`, which the MCP server module
  imports; the unpinned spec silently resolved into a broken install. Pin
  resolves to mcp 1.29.0, the final maintained 1.x release cut on the
  same day as 2.0.0 for exactly this scenario. Migration to the mcp 2.0.0
  API shape is planned for v1.1. ([#127])
- **MCP `remember` now runs the same graph extraction as REST/file
  ingestion.** Previously the MCP surface inserted a chunk directly and
  skipped `_run_extraction`, so MCP-stored memories were searchable via
  `recall` but silently absent from every `/api/graph/*` surface. Extraction
  errors are still swallowed internally so the chunk stays committed even
  if spaCy fails. ([#128])
- **MCP `remember` rejects empty text and text over 1,000,000 characters
  at the boundary.** Matches `IngestTextRequest`'s `min_length=1,
  max_length=1_000_000` on the REST surface. ([#128])
- **`since` timestamp semantics on `POST /api/search` and MCP `recall`.**
  Offset-aware inputs like `2026-01-01T00:00:00-05:00` are now converted
  to UTC via `.astimezone(UTC)` instead of relabelled with `.replace(tzinfo=UTC)`.
  Naive inputs still assume UTC per the documented API contract. ([#124])

### Fixed

- **`recall(spaces=["unknown_name"])` no longer widens the search to every
  space.** `resolve_space_names()` returns `[]` for unknown names, and every
  `hybrid_search` caller previously collapsed `[]` back to `None` via
  `space_ids or None`, so the space filter silently dropped out. `[]` now
  propagates through `_build_where_clause`, which emits a hard `false`
  predicate. ([#122])
- **`EMBEDDING_DIMENSIONS` config mismatch fails fast at startup instead of
  crashing on the first embedding INSERT/SELECT.** The pool-init path now
  queries `pg_attribute` for the actual `vector(N)` dimension on
  `chunks.embedding` and raises with a clear message on mismatch. Skips
  cleanly on a fresh install where `chunks` doesn't exist yet. ([#123])
- **Forgotten chunks no longer leak through `/api/graph/*` endpoints.**
  `DELETE /api/chunks/{id}` marks a chunk as forgotten; the four graph
  endpoints (list entities, entity detail, list relationships, visualize)
  now apply the same predicate that search already uses. `mention_count`
  reports only live mentions, entities with zero live mentions drop out
  of listings, and forgotten-chunk preview text never surfaces via entity
  detail. Relationships with `chunk_id IS NULL` (future manual/LLM tagging)
  are preserved. ([#129])

### Fixed (P0 emergency)

- **v1.0.9 MCP-only Docker image was dead on arrival.**
  `ghcr.io/mihaibuilds/memory-vault-mcp:1.0.9` crashed at startup with
  `ModuleNotFoundError: No module named 'mcp.server.fastmcp'` for every
  fresh pull since 2026-08-02. Root cause was the unpinned `mcp` spec
  drifting into 2.0.0. Fresh source installs hit the same crash. Workaround
  for anyone on v1.0.9: use `ghcr.io/mihaibuilds/memory-vault-mcp:1.0.8`
  or pin `mcp<2.0.0` in your own env. Fixed in v1.0.10 via the mcp pin
  above. ([#126], [#127])

### Contributors

- [@lcj-codex-coder] (Leonard Janke — lcjanke2020, working with GPT-5.6-Sol
  through OpenAI Codex) — reported [#100], [#105], [#109], [#114]

## [1.0.9] — 2026-08-02

### Added

- Pool checkout validation: `AsyncConnectionPool` runs a liveness check on
  checkout so connections that died while idle are discarded and replaced
  transparently instead of surfacing as query failures. Fixes the "server
  closed the connection unexpectedly" errors common on remote-Postgres
  deployments. ([#118])

### Changed

- Every runtime and release surface now agrees with the released tag.
  `/api/health`, FastAPI `/openapi.json`, `docker-compose.yml`,
  `server.json`, and `memory-vault diagnose` all report the real installed
  package version, resolved via `importlib.metadata`. The release workflow
  now blocks a tag whose descriptors drift. ([#97], [#120])

### Contributors

- [@hmodes] — [#118] (pool checkout validation)
- [@lcj-codex-coder] (Leonard Janke — lcjanke2020, working with GPT-5.6-Sol
  through OpenAI Codex) — [#97] (version drift report)

## [1.0.8] — 2026-07-23

Maintenance release; no changelog was published at tag time.

## [1.0.7] — 2026-07-19

### Fixed

- **`memory-vault` CLI is now pip-installable.** Every subcommand
  (`status`, `migrate`, `ingest`, `search`, `mcp`, `api`, `token`, `space`,
  `diagnose`) failed with `ModuleNotFoundError: No module named 'src'`
  immediately on invocation for pip-installed deployments across every
  prior 1.0.x release. Docker deployments accidentally kept working via a
  `PYTHONPATH=/app` workaround. Refactored to a proper `memory_vault`
  package, updated the entry point, bundled the SQL migrations with the
  wheel, and removed the Docker workaround so containers and pip installs
  share a single install path. ([#76], [#84])
- **`pyproject.toml` version now tracks the git tag.** The package
  reported version 0.4.0 regardless of which 1.0.x tag was installed
  because the release workflow never bumped the version file. A one-time
  correction to 1.0.7 plus a version-guard job in `release.yml` prevent
  drift going forward. ([#75], [#78])
- **Docker base image aligned to Python 3.13.** spaCy has no cp314 wheel
  yet, so every `docker build` failed at `pip install`. Base image
  downgraded from `python:3.14-slim` to `python:3.13-slim`, matching the
  CI pytest matrix. `dependabot.yml` ignore entry added to prevent
  automatic bumps back to the bleeding edge. ([#79], [#80])

### Known issues

- Windows `ProactorEventLoop` startup warnings on stdio MCP deployments
  with remote Postgres. Reproduces on Windows 11 + Python 3.12 + psycopg
  async pool. Needs a controlled repro before choosing the fix. ([#77])

### Contributors

- [@git-pharos] — [#74] (diagnostic bundle that exposed three of the four
  fixes above)

## [1.0.6] — 2026-05-18

### Fixed

- Corrected casing of the `io.modelcontextprotocol.server.name` OCI
  annotation on `memory-vault-mcp` from lowercase (`mihaibuilds`) to the
  actual GitHub org login casing (`MihaiBuilds`). MCP Registry publish was
  blocked with a 403 until this was corrected.

## [1.0.5] — 2026-05-18

### Added

- `LABEL io.modelcontextprotocol.server.name="io.github.mihaibuilds/memory-vault"`
  on `Dockerfile.mcp`. The official MCP Registry uses this OCI annotation
  to verify that the publisher of a `server.json` actually controls the
  image. Prep for registry submission.

## [1.0.4] — 2026-05-18

### Added

- **New `memory-vault-mcp` Docker image** — thin MCP-only image that ships
  the MCP stdio server and nothing else. Connects to an external
  Postgres+pgvector via env vars. Intended for direct `mcp.json`
  configurations, MCP catalog registry submissions, and larger Compose
  setups with shared Postgres. Multi-arch (amd64 + arm64). The existing
  all-in-one image continues unchanged.

## [1.0.3] — 2026-05-17

### Changed

- **Docker base images:** `python:3.11-slim` → `python:3.14-slim`,
  `node:20-slim` → `node:26-slim`. (Reverted to `python:3.13-slim` in
  v1.0.7 due to spaCy wheel gap.)
- **CI runners aligned** to the same Python 3.14 / Node 26 as the shipped
  image (previously tests ran on 3.11/20 while the image shipped on
  3.14/26 — a tested-vs-shipped divergence closed).
- **Tailwind CSS v3 → v4** on the web dashboard: full migration via the
  official codemod, including PostCSS plugin rename and config-as-CSS via
  `@theme`.
- **GitHub Actions:** `actions/checkout@v4 → v6`, `actions/cache@v4 → v5`,
  plus 5 Dependabot-bumped CI actions.
- Documented 4 intentional empty-`except` fallback sites (CodeQL "Empty
  except") in `chat.py`, `adapters/base.py`, `diagnose.py`,
  `tests/test_chat_api.py`. No behavior change — comments only.

## [1.0.2] — 2026-05-08

### Security

- **Path traversal in SPA fallback (High, `py/path-injection`).** The
  unauthenticated SPA fallback route accepted user-controlled paths and
  composed them with the static directory, allowing requests like
  `GET /../../etc/passwd` to escape. Fixed via a `_safe_static_path`
  helper using `os.path.commonpath` + `os.path.realpath` plus
  pre-composition rejection of empty / null-byte / leading-slash /
  explicit-traversal inputs. Three independent layers of defense.
  (CodeQL alert 2 + 3; [#19])
- **Information exposure in chat stream (Medium, `py/stack-trace-exposure`).**
  The inner SSE error handler in `/api/chat/stream` interpolated raw
  exception text into the response. Fixed: server-side `logger.exception(...)`,
  generic client message. (CodeQL alert 1; [#19])

### Notes

- Three CodeQL partial-SSRF findings on the `llm_url` field in
  `ChatRequest` were dismissed as architectural — Memory Vault is
  single-tenant self-hosted with bearer-token auth, and the `llm_url`
  field is intentional operator configuration. Hardening guidance for
  non-default deployments tracked in [#18] for v1.1.

## [1.0.1] — 2026-05-07

### Fixed

- **`docker-compose.yml` now references the published image** instead of
  building from source. First-run on a fresh clone is now ~30 seconds
  (image pull) instead of ~5 minutes (local build). The README's
  "one-command Docker" promise is now actually one command.

## [1.0.0] — 2026-05-07

### Added

Memory Vault v1.0 — first stable release. A long-term memory layer for AI
assistants and the apps you build on top of them.

- **Hybrid search** — pgvector HNSW + tsvector GIN, merged with Reciprocal
  Rank Fusion.
- **MCP server** — `recall`, `remember`, `forget`, `status` for Claude
  Desktop / Claude Code.
- **Knowledge graph** — spaCy NER + co-occurrence, no LLM cost, Cytoscape
  visualization.
- **Local LLM chat** — LM Studio with a sources panel showing retrieved
  chunks per answer.
- **REST API** — FastAPI, bearer auth, OpenAPI at `/docs`.
- **Memory spaces** — namespacing for different contexts (work, personal,
  projects).
- **One-command Docker** — multi-arch image (linux/amd64 + linux/arm64).
- 163 tests passing in CI against a real Postgres + pgvector service
  container.

[Unreleased]: https://github.com/MihaiBuilds/memory-vault/compare/v1.1.0...HEAD
[1.1.0]: https://github.com/MihaiBuilds/memory-vault/compare/v1.0.10...v1.1.0
[1.0.10]: https://github.com/MihaiBuilds/memory-vault/compare/v1.0.9...v1.0.10
[1.0.9]: https://github.com/MihaiBuilds/memory-vault/compare/v1.0.8...v1.0.9
[1.0.8]: https://github.com/MihaiBuilds/memory-vault/compare/v1.0.7...v1.0.8
[1.0.7]: https://github.com/MihaiBuilds/memory-vault/compare/v1.0.6...v1.0.7
[1.0.6]: https://github.com/MihaiBuilds/memory-vault/compare/v1.0.5...v1.0.6
[1.0.5]: https://github.com/MihaiBuilds/memory-vault/compare/v1.0.4...v1.0.5
[1.0.4]: https://github.com/MihaiBuilds/memory-vault/compare/v1.0.3...v1.0.4
[1.0.3]: https://github.com/MihaiBuilds/memory-vault/compare/v1.0.2...v1.0.3
[1.0.2]: https://github.com/MihaiBuilds/memory-vault/compare/v1.0.1...v1.0.2
[1.0.1]: https://github.com/MihaiBuilds/memory-vault/compare/v1.0.0...v1.0.1
[1.0.0]: https://github.com/MihaiBuilds/memory-vault/releases/tag/v1.0.0

[#19]: https://github.com/MihaiBuilds/memory-vault/issues/19
[#47]: https://github.com/MihaiBuilds/memory-vault/pull/47
[#74]: https://github.com/MihaiBuilds/memory-vault/issues/74
[#75]: https://github.com/MihaiBuilds/memory-vault/issues/75
[#76]: https://github.com/MihaiBuilds/memory-vault/issues/76
[#77]: https://github.com/MihaiBuilds/memory-vault/issues/77
[#78]: https://github.com/MihaiBuilds/memory-vault/issues/78
[#79]: https://github.com/MihaiBuilds/memory-vault/issues/79
[#80]: https://github.com/MihaiBuilds/memory-vault/issues/80
[#84]: https://github.com/MihaiBuilds/memory-vault/issues/84
[#91]: https://github.com/MihaiBuilds/memory-vault/pull/91
[#92]: https://github.com/MihaiBuilds/memory-vault/pull/92
[#93]: https://github.com/MihaiBuilds/memory-vault/pull/93
[#94]: https://github.com/MihaiBuilds/memory-vault/pull/94
[#95]: https://github.com/MihaiBuilds/memory-vault/pull/95
[#96]: https://github.com/MihaiBuilds/memory-vault/pull/96
[#97]: https://github.com/MihaiBuilds/memory-vault/issues/97
[#100]: https://github.com/MihaiBuilds/memory-vault/issues/100
[#105]: https://github.com/MihaiBuilds/memory-vault/issues/105
[#109]: https://github.com/MihaiBuilds/memory-vault/issues/109
[#114]: https://github.com/MihaiBuilds/memory-vault/issues/114
[#118]: https://github.com/MihaiBuilds/memory-vault/issues/118
[#120]: https://github.com/MihaiBuilds/memory-vault/issues/120
[#122]: https://github.com/MihaiBuilds/memory-vault/issues/122
[#123]: https://github.com/MihaiBuilds/memory-vault/issues/123
[#124]: https://github.com/MihaiBuilds/memory-vault/issues/124
[#125]: https://github.com/MihaiBuilds/memory-vault/issues/125
[#126]: https://github.com/MihaiBuilds/memory-vault/issues/126
[#127]: https://github.com/MihaiBuilds/memory-vault/issues/127
[#128]: https://github.com/MihaiBuilds/memory-vault/issues/128
[#129]: https://github.com/MihaiBuilds/memory-vault/issues/129
[#134]: https://github.com/MihaiBuilds/memory-vault/pull/134
[#135]: https://github.com/MihaiBuilds/memory-vault/pull/135
[#136]: https://github.com/MihaiBuilds/memory-vault/pull/136

[@hmodes]: https://github.com/hmodes
[@git-pharos]: https://github.com/git-pharos
[@lcj-codex-coder]: https://github.com/lcj-codex-coder
[@skorten]: https://github.com/skorten
