# Changelog

All notable changes to Memory Vault are documented here.

The format follows [Keep a Changelog 1.1.0](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [1.5.0] — 2026-09-05

Eight additions, most of them about doing something with a graph you have
already built: reading the memories behind a node, filtering by more than one
type at a time, and merging the entities that extraction split apart. Plus a
Windows fix that had been waiting on a reproduction.

Nothing here requires action on upgrade. Every new parameter is optional, and
every existing call behaves as it did.

### Added

- **Merge entities the extractor kept apart.** Extraction is literal and
  per-occurrence, so one person arrives as "Alice", "Alice Smith" and
  "A. Smith". `POST /api/graph/entities/merge` folds one into another in a
  single transaction. Deciding that two entities are the same is a judgement
  call, so it is offered rather than guessed at. Merging across spaces is
  refused: entities are per-space by design, and a cross-space merge would move
  data between spaces without saying so.
- **Read the memories behind a graph node.** `GET /api/chunks?entity_id=…`
  returns the chunks that mention an entity, and the graph's node panel links
  into Browse with that filter applied. The node used to be a dead end — you
  could see that something mattered and how often it came up, but not read what
  it came from.
- **Filter the graph by several types at once.** `?type=Person,Tool` on the
  entity, relationship and visualization endpoints. Selecting two types meant
  two requests and no way to see both together. A single value still behaves
  exactly as before.
- **Delete a space.** `DELETE /api/spaces/{name}` removes one that holds
  nothing, with a two-step delete on the dashboard. Spaces could be created but
  never removed, so a typo was permanent. A space with contents is refused
  rather than emptied — and "empty" means no memories *and* no graph entities,
  since deleting a space cascades into its entities.
- **Upload several files in one request.** `POST /api/ingest/files` answers per
  file: one malformed file in a batch of thirty does not discard the other
  twenty-nine, and the response says which one to fix. Bounded per file, in
  aggregate, and by count.
- **Tune search breadth per query.** `ef_search` on `/api/search` and the MCP
  `recall` tool widens the HNSW search for one query without changing the
  server default for everyone. Bounded 1–1000.
- **The vector index is warmed at start-up.** `memory-vault warm-index` runs
  after migrations, so the first real search is not the one that pays to read
  the index off disk. Warming never blocks start-up: a container that refuses to
  start because an optimisation failed is worse than a slow first query.

### Fixed

- **Memory Vault now starts cleanly on Windows.** Windows defaults to an event
  loop psycopg refuses to use, so every connection attempt failed instantly and
  the pool retried — a burst of identical warnings and a multi-second delay
  before anything worked. A compatible loop is now selected at import.
  Reproduced on Windows 11 beforehand and measured after: three warnings and a
  pool timeout became zero warnings. Other platforms are untouched. ([#77])

### Contributors

- Rivestack suggested both the per-query `ef_search` knob and warming the vector
  index at start-up.

[#77]: https://github.com/MihaiBuilds/memory-vault/issues/77

## [1.4.0] — 2026-08-23

Ten bug fixes and one new capability. Most of these are the kind that only
surface once you have been using Memory Vault for a while: a budget that was
not really a cap, a chunk limit that was not really a limit, forgotten memories
that never actually went away.

Nothing here requires action on upgrade.

### Added

- **Forgotten memories can be purged.** `forget` is a soft delete so a memory
  can be recovered, but nothing ever removed the rows afterwards — and editing a
  memory means forget plus remember, so a vault that is edited often grew a dead
  row per edit. `purge_forgotten(older_than_days=30)` over MCP, or
  `memory-vault purge-forgotten` on the command line, deletes them for good.
  It is deliberately not automatic: Memory Vault runs no timer, so nothing
  deletes your memories unless you ask. The default only removes what was
  forgotten at least a month ago, so purging right after an accidental forget
  still spares it. ([#74])
- **`memory_status` reports `forgotten_chunks`.** The number was derivable by
  subtracting, but naming it is what tells you whether purging is worth doing.
  ([#74])

### Fixed

- **A single oversized result could blow the token budget.** Both the chat and
  MCP budget helpers admitted one arbitrarily large result — 2540 tokens against
  a 200 budget in the reported case, and the caller was not even told it had
  happened. Keeping at least one result is deliberate, since an answer with no
  context is worse than one with trimmed context; sending it whole was not. The
  final result is now trimmed to what is left. ([#99])
- **`max_words` was not an upper bound.** A sentence longer than the limit had
  no sentence boundary to split on, so it passed through whole — and because the
  flush was guarded on the accumulator being non-empty, an oversized *first*
  sentence escaped even with more text after it. Word splitting is now the
  floor under everything else. ([#103])
- **Uploaded files recorded the server's temporary path as their source.** Every
  chunk from an upload pointed at something like `/tmp/tmpabcd1234.md`, deleted
  the moment the request ended. Uploads now record the filename you sent — which
  also repairs re-upload deduplication, since a path that changes every request
  could never match. ([#101])
- **Repeated entity occurrences collapsed to one graph mention.** "Alice met
  Alice" recorded one mention rather than two. Entity identity is still
  deduplicated — repeated mentions resolve to one node — but each occurrence now
  keeps its own offsets. Expect mention counts to rise as content is
  re-ingested. ([#110])
- **An empty environment variable crashed at start-up.** `os.getenv` falls back
  to its default only when a key is absent, so `DB_PORT=""` reached `int()` and
  raised. Config generated from a manifest emits every declared key, empty where
  it has no value, which made this a first-run failure. Empty now means unset,
  and a malformed value names the setting instead of failing from inside the
  standard library. ([#181])
- **Concurrent creation of the same space returned 500.** Two callers could both
  see no existing row and both insert; the loser hit the unique constraint. The
  insert is now authoritative and the loser gets the same 409 as any other
  duplicate. ([#112])
- **A malformed UUID in a path returned 500.** `not-a-valid-identifier` reached
  PostgreSQL and became a server error. Those routes now reject it at the
  boundary without opening a database connection. ([#102])
- **Adding a file after a finished batch re-uploaded the completed ones.** The
  submit loop ran over every file regardless of status, so a file already
  ingested went again — while the button correctly offered to ingest only the
  new one. ([#106])
- **Stopping a chat left the answer marked as streaming.** The input unlocked
  and the request ended, but the turn kept showing "Thinking…" indefinitely. It
  now reaches a terminal state while keeping whatever text had arrived. ([#107])

### Contributors

- [@lcj-codex-coder] (Leonard Janke — lcjanke2020, working with GPT-5.6-Sol
  through OpenAI Codex) — reported [#99], [#101], [#102], [#103], [#106],
  [#107], [#110], [#112]
- [@git-pharos] — reported [#74], the unbounded growth that `purge_forgotten`
  answers

## [1.3.0] — 2026-08-23

Least privilege. The containers no longer run as root, the database role that
serves requests can no longer change your schema, API tokens can expire, and the
threat model that describes all of it is now written down.

Nothing here requires action on upgrade. The container hardening applies
automatically; the database roles and token expiry are opt-in and existing
deployments keep working unchanged.

### Added

- **A published threat model.** [`docs/threat-model.md`](docs/threat-model.md)
  sets out what Memory Vault protects, what it assumes about its environment, and
  what it explicitly does **not** defend against — stolen tokens carry full
  access, spaces are not a security boundary, content is stored unencrypted, and
  prompt injection is not filtered. It also covers hardening a deployment that
  goes beyond a single machine: token hygiene, network scoping, egress policy,
  TLS termination, and rate limiting. ([#18])
- **Optional expiry for API tokens.** `memory-vault token create <name>
  --expires-in-days N`. A token created without an expiry never lapses, so every
  token issued before this release keeps working. `token list` gains an
  `EXPIRES` column and marks lapsed tokens. An expired token is reported
  differently from a revoked one, because an operator debugging a 401 needs to
  know which.
- **Least-privilege database roles.** Migration 009 defines
  `memory_vault_app` (read and write rows), `memory_vault_readonly` (select
  only), and `memory_vault_migrator` (schema changes). They are group roles with
  no login of their own, so nothing changes until you adopt them —
  `DB_MIGRATION_USER` and `DB_MIGRATION_PASSWORD` let migrations run as a role
  the serving connection does not use. Adoption steps are in the threat model.

### Changed

- **The containers run as a non-root user and need no writable filesystem.**
  Both images create a system user (uid 10001), and the shipped compose file adds
  a read-only root filesystem, drops all Linux capabilities, and sets
  `no-new-privileges`. The embedding model is now baked into the image at build
  time, which makes that possible and also removes a network round trip from the
  first query after every start.

### Fixed

- **A read-only container no longer refuses to start when it cannot write its
  log file.** The existing guard covered creating the log directory, but the
  directory ships inside the image — so the failure landed on opening the file
  instead, and a deployment with a read-only root filesystem and no log volume
  crashed at boot. It now warns and continues logging to stderr.
- **The threat model's description of the authentication boundary was wrong.**
  `/docs`, `/redoc` and `/openapi.json` are unauthenticated and exempt from rate
  limiting, which the document did not say. No memory content is exposed and
  every documented operation still requires a token, but on a publicly reachable
  host they describe the API to anonymous visitors. The document now lists them
  and suggests blocking them at a reverse proxy.

### Security

- **Public artifacts are scanned in CI.** Local git hooks only ever see a command
  line, so anything written through the GitHub API — a pull request body passed
  as a file, an issue edited in the browser, release notes — never reached one.
  A workflow now scans pull request and issue text, release notes, and added
  lines in a diff. It fails closed: a missing pattern file, an empty one, or a
  malformed pattern all fail the run.

### Dependencies

- ruff `>=0.16.3`, setuptools `>=84.0.0`, uvicorn `>=0.52.3`, spacy `>=3.8.15`
  ([#158], [#160], [#169])
- Eight web dependencies updated as a group, including cytoscape 3.34.1 and
  vite 8.2.2 ([#170])

## [1.2.1] — 2026-08-18

Patch release. Two retrieval and observability fixes, both reported and fixed
by an outside contributor running Memory Vault at scale.

### Fixed

- **Importance and recency boosts no longer bury exact matches.** The boosts
  were added to the RRF score, but the maximum boost (0.20) was roughly
  eighteen times the entire spread between a rank-1 and a rank-50 result
  (~0.011). Relevance was effectively the tie-breaker and importance the
  primary sort key, so a recent high-importance note could outrank an exact
  match on an unrelated query. The boosts now scale the retrieval score
  instead of being added to it: they still order results of comparable
  relevance, but can no longer invert a clear rank difference. ([#163], [#165])
- **MCP `recall` now records queries in `query_log`.** The REST search endpoint
  was the only caller of the query logger, so on an MCP-only deployment
  `memory_status` and `memory://stats` always reported `queries_24h: 0` and a
  null average latency however much the vault was used. ([#164])

### Contributors

- [@ghawley365] (Gary Hawley) — reported and fixed [#163] (via [#165]) and
  [#164]

## [1.2.0] — 2026-08-17

Correctness release. Nine bugs fixed, two features added, and the MCP SDK
migrated to 2.x. Most of the bugs are concurrency or failure-path defects that
only show up under load or once something else has already gone wrong: a
memory stored twice, a retry that duplicated a file, a connection pool that
never recovered, a process that aborted outright.

Four migrations ship here (004–007). They apply automatically on start.
Existing rows are not rewritten and no backfill runs, so upgrading does not
touch data already stored.

### Fixed

- **Concurrent `remember` calls no longer store the same memory twice.** The
  duplicate check and the insert were separate statements with nothing holding
  a lock between them, so two simultaneous calls could both pass the check and
  both commit. A unique index now backs the guarantee and the insert resolves
  the conflict itself. Migration 004 collapses duplicates left behind by the
  race before creating the index, so a database that already hit the bug still
  upgrades. ([#111])
- **A failed file ingestion no longer leaves partial chunks behind, and
  retrying no longer duplicates them.** Each chunk was committed on its own, so
  a failure part-way through left the earlier chunks stored with nothing
  recording that the file was incomplete. A file's chunks now share one
  transaction, and migration 005 gives each chunk an identity so re-ingesting
  the same file is a no-op rather than a second copy. A document that
  legitimately repeats a passage still stores both copies. ([#115])
- **Concurrent embedding calls no longer abort the process.** The embedding
  model is a single shared object whose forward pass is not safe to run from
  several threads at once, so two simultaneous requests could kill the process
  rather than raise an error. Embedding is now serialised, and the lazy model
  load no longer races. ([#148])
- **A connection pool that fails to open is no longer cached.** If the database
  was not accepting connections at start-up, the failed pool was stored and
  handed to every later caller, so the process never recovered even once the
  database came back. ([#117])
- **Database credentials containing URI-reserved characters now work.** A
  password containing `/`, `@`, `:` or `#` broke connection-string parsing.
  Connection parameters are now assembled by the driver instead of being
  interpolated into a URI. ([#113])
- **`/api/health` returns 503 when the database is unreachable.** It returned
  200 with a `degraded` body, and the Docker healthcheck only inspects the
  status code — so a container whose database had gone away still reported
  itself healthy. The body keeps its shape for operators reading it directly.
  ([#108])
- **Embedding no longer blocks the event loop.** Inference ran synchronously
  inside async request handlers, so one embedding call stalled every other
  request in flight. Four call sites now run it in a worker thread. ([#116])
- **`memory-vault ingest` exits non-zero when any file fails.** It printed the
  failure count and exited 0, so scripts and CI treated a failed ingestion as a
  success. The individual errors are now printed as well. ([#104])
- **Streamed answers are no longer discarded when a `</think>` tag is split
  across chunks.** Inside a thinking block the parser cleared its whole buffer
  whenever it did not find a complete closing tag, throwing away a trailing
  fragment that the next chunk would have completed — and the answer with it.
  The same fix covers a second case where a response ending in a `<` character
  was truncated. ([#98])

### Added

- **Spaces are created on first write.** Ingesting into a space that did not
  exist yet returned 404, so a space had to be created in a separate request
  before anything could be stored in it. `POST /api/ingest/text` and
  `POST /api/ingest/file` now create it. The name still has to be one the API
  would accept from an explicit create, so a typo fails rather than quietly
  producing a near-identical space. The MCP `remember` tool deliberately still
  refuses unknown spaces and lists the ones that exist. ([#153])
- **Memories can be moved between spaces.** `move_memory` on the MCP surface
  and `POST /api/chunks/{id}/move` change a memory's space without re-embedding
  it, and rebuild its knowledge-graph entries in the target space so the graph
  and search agree about where it lives. The target space must already exist.
  ([#154])

### Changed

- **The MCP SDK is now 2.x** (`mcp>=2.0.0,<3.0.0`). 2.0.0 removed
  `mcp.server.fastmcp`, which is why the SDK had been pinned below it; the
  class moved to `mcp.server.mcpserver` and was renamed. No behaviour change —
  the server exposes the same tools over the same transport. ([#141])
- **Knowledge-graph queries read through database views.** Excluding forgotten
  memories was restated by hand in every graph query, which worked but left the
  next one a missed clause away from surfacing them. Migration 007 states the
  rule once. No behaviour change. ([#155])

### Contributors

- [@lcj-codex-coder] (Leonard Janke — lcjanke2020, working with GPT-5.6-Sol
  through OpenAI Codex) — reported [#98], [#104], [#108], [#111], [#113],
  [#115], [#116], [#117]
- [@gatesl] — [#52] (the space-lifecycle design that `_ensure_space` and
  `move_memory` are built on)

## [1.1.1] — 2026-08-16

Small patch release: three dependency bumps merged one-by-one with per-PR
throwaway-stack smoke tests. No code authored by MV. No behavior changes to the
runtime, no schema changes, no migrations. Safe upgrade.

### Changed

- **`vite` bumped to `^8.2.1`** (was `^8.2.0`) in the web dev-deps group. Docker
  build smoke passed (Node/Vite bundle stage + Python image stage). ([#138])
- **`uvicorn[standard]` requirement raised to `>=0.52.2,<1`** (was `>=0.51.0,<1`).
  Resolved to 0.52.3 in-container. ASGI server smoke: throwaway stack boots
  cleanly, `/api/health` responds. ([#140])
- **`sentence-transformers` requirement raised to `>=5.7.0,<6`** (was
  `>=5.6.1,<6`). Semantic-search smoke: two-topic ranking test returns
  byte-identical similarity scores to v1.1.0 — no embedding drift, users don't
  need to re-embed. ([#139])

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

[Unreleased]: https://github.com/MihaiBuilds/memory-vault/compare/v1.4.0...HEAD
[1.4.0]: https://github.com/MihaiBuilds/memory-vault/compare/v1.3.0...v1.4.0
[1.3.0]: https://github.com/MihaiBuilds/memory-vault/compare/v1.2.1...v1.3.0
[1.2.1]: https://github.com/MihaiBuilds/memory-vault/compare/v1.2.0...v1.2.1
[1.2.0]: https://github.com/MihaiBuilds/memory-vault/compare/v1.1.1...v1.2.0
[1.1.1]: https://github.com/MihaiBuilds/memory-vault/compare/v1.1.0...v1.1.1
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

[#18]: https://github.com/MihaiBuilds/memory-vault/issues/18
[#19]: https://github.com/MihaiBuilds/memory-vault/issues/19
[#47]: https://github.com/MihaiBuilds/memory-vault/pull/47
[#52]: https://github.com/MihaiBuilds/memory-vault/pull/52
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
[#98]: https://github.com/MihaiBuilds/memory-vault/issues/98
[#99]: https://github.com/MihaiBuilds/memory-vault/issues/99
[#101]: https://github.com/MihaiBuilds/memory-vault/issues/101
[#102]: https://github.com/MihaiBuilds/memory-vault/issues/102
[#103]: https://github.com/MihaiBuilds/memory-vault/issues/103
[#106]: https://github.com/MihaiBuilds/memory-vault/issues/106
[#107]: https://github.com/MihaiBuilds/memory-vault/issues/107
[#110]: https://github.com/MihaiBuilds/memory-vault/issues/110
[#112]: https://github.com/MihaiBuilds/memory-vault/issues/112
[#181]: https://github.com/MihaiBuilds/memory-vault/issues/181
[#100]: https://github.com/MihaiBuilds/memory-vault/issues/100
[#104]: https://github.com/MihaiBuilds/memory-vault/issues/104
[#105]: https://github.com/MihaiBuilds/memory-vault/issues/105
[#108]: https://github.com/MihaiBuilds/memory-vault/issues/108
[#109]: https://github.com/MihaiBuilds/memory-vault/issues/109
[#111]: https://github.com/MihaiBuilds/memory-vault/issues/111
[#113]: https://github.com/MihaiBuilds/memory-vault/issues/113
[#114]: https://github.com/MihaiBuilds/memory-vault/issues/114
[#115]: https://github.com/MihaiBuilds/memory-vault/issues/115
[#116]: https://github.com/MihaiBuilds/memory-vault/issues/116
[#117]: https://github.com/MihaiBuilds/memory-vault/issues/117
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
[#138]: https://github.com/MihaiBuilds/memory-vault/pull/138
[#139]: https://github.com/MihaiBuilds/memory-vault/pull/139
[#140]: https://github.com/MihaiBuilds/memory-vault/pull/140
[#141]: https://github.com/MihaiBuilds/memory-vault/pull/141
[#148]: https://github.com/MihaiBuilds/memory-vault/issues/148
[#153]: https://github.com/MihaiBuilds/memory-vault/pull/153
[#154]: https://github.com/MihaiBuilds/memory-vault/pull/154
[#155]: https://github.com/MihaiBuilds/memory-vault/pull/155
[#163]: https://github.com/MihaiBuilds/memory-vault/issues/163
[#164]: https://github.com/MihaiBuilds/memory-vault/pull/164
[#165]: https://github.com/MihaiBuilds/memory-vault/pull/165
[#158]: https://github.com/MihaiBuilds/memory-vault/pull/158
[#160]: https://github.com/MihaiBuilds/memory-vault/pull/160
[#169]: https://github.com/MihaiBuilds/memory-vault/pull/169
[#170]: https://github.com/MihaiBuilds/memory-vault/pull/170

[@hmodes]: https://github.com/hmodes
[@git-pharos]: https://github.com/git-pharos
[@lcj-codex-coder]: https://github.com/lcj-codex-coder
[@gatesl]: https://github.com/gatesl
[@skorten]: https://github.com/skorten
[@ghawley365]: https://github.com/ghawley365
