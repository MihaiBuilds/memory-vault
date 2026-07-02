# Memory Vault — Architecture Overview

This document describes the technical architecture of Memory Vault as it ships in **v1.0**. It covers the core components, the schema, how clients connect, and the design decisions behind the trade-offs.

For user-facing setup and feature docs, see the [README](README.md). For contribution flow, see [CONTRIBUTING.md](CONTRIBUTING.md). For the threat model and security posture, see [SECURITY.md](SECURITY.md).

---

## High-Level Architecture

```
┌─────────────────────────────────────────────────────────────┐
│                          Clients                            │
│                                                             │
│   Claude (MCP)    Web Dashboard    REST API     CLI         │
└────────┬──────────────┬───────────────┬──────────┬──────────┘
         │              │               │          │
         │              └───── HTTP ────┘          │
         │                     │                   │
         │            ┌────────▼────────┐          │
         │            │    FastAPI      │          │
         │            │  (REST + WebUI) │          │
         │            └────────┬────────┘          │
         │                     │                   │
    ┌────▼─────────┐           │              ┌────▼──────┐
    │  MCP Server  │           │              │ memory-   │
    │  (stdio)     │           │              │ vault CLI │
    └──────┬───────┘           │              └────┬──────┘
           │                   │                   │
           └───────────────────┼───────────────────┘
                               │
              ┌────────────────┼────────────────┐
              │                │                │
    ┌─────────▼──────┐ ┌───────▼────────┐ ┌─────▼─────────┐
    │ Ingestion      │ │ Hybrid Search  │ │ Knowledge     │
    │ Pipeline       │ │ Engine         │ │ Graph         │
    │ - Markdown     │ │ - Vector HNSW  │ │ - spaCy NER   │
    │ - Plaintext    │ │ - Full-text    │ │ - Co-occur    │
    │ - Claude JSON  │ │ - RRF merge    │ │ - Cytoscape   │
    └─────────┬──────┘ └───────┬────────┘ └─────┬─────────┘
              │                │                │
              └────────────────┴────────────────┘
                               │
                  ┌────────────▼────────────┐
                  │      PostgreSQL 16      │
                  │      + pgvector         │
                  │                         │
                  │  memory_spaces          │
                  │  chunks                 │
                  │  api_tokens             │
                  │  entities               │
                  │  relationships          │
                  │  entity_mentions        │
                  │  query_log              │
                  └─────────────────────────┘
```

The same memory layer is reachable from four equal first-class clients: **MCP** (Claude Desktop / Claude Code), the **REST API**, the bundled **web dashboard**, and the **`memory-vault` CLI**. The dashboard is one consumer of the API — not the API itself.

---

## Storage — PostgreSQL 16 + pgvector

Everything lives in a single Postgres database with the `pgvector` extension. Migrations are versioned and forward-only (`migrations/001_initial_schema.sql`, `002_api_tokens.sql`, `003_knowledge_graph.sql`) and run automatically on first start.

### Tables

**`memory_spaces`** — namespaces for organizing memories.
- `id` SERIAL primary key
- `name` TEXT unique (lowercase, hyphens allowed; e.g. `default`, `work`, `learning`)
- `description`, `created_at`

**`chunks`** — the primary memory unit. One row per ingested chunk.
- `id` UUID primary key
- `space_id` references `memory_spaces`
- `content` TEXT — the raw text
- `embedding` vector(384) — `all-MiniLM-L6-v2` embedding
- `content_tsv` tsvector — auto-populated by trigger from `content`
- `source` TEXT — origin (file path, URL, MCP tool that wrote it)
- `speaker` TEXT — `'human'`, `'assistant'`, or null
- `metadata` JSONB
- `importance` FLOAT (default 0.5)
- `chunk_index` INTEGER — position within a multi-chunk source
- `created_at`, `updated_at`

Indexes: HNSW on `embedding` (vector cosine), GIN on `content_tsv` (full-text), btree on `space_id` and `created_at`, GIN on `metadata` (jsonb_path_ops).

**`api_tokens`** — bearer-auth tokens for the REST API and dashboard.
- `id` UUID primary key
- `name` TEXT — friendly label
- `token_hash` TEXT unique — SHA-256 hash of the plaintext token (never stored in clear)
- `token_prefix` TEXT — first 11 chars (`mv_xxxxxxxx`) for identification in `token list`
- `created_at`, `last_used_at`, `revoked_at`

Tokens are 32 random bytes from `secrets.token_urlsafe`; lookup uses `hmac.compare_digest` for constant-time comparison.

**`entities`** — knowledge graph nodes (one per `(lower(name), type, space_id)` triple).
- `id` UUID primary key
- `name` TEXT, `type` TEXT (`person`, `project`, `tool`, `concept`)
- `space_id` references `memory_spaces` — entities are deduplicated per space, not globally
- `properties` JSONB
- `created_at`, `updated_at`

**`relationships`** — knowledge graph edges. One row per `(source_entity, target_entity, type)` derived from co-occurrence.
- `id` UUID primary key
- `source_entity_id`, `target_entity_id` — both reference `entities`
- `type` TEXT — `related_to` in v1.0
- `strength` FLOAT — co-occurrence weight, increments with repeated co-occurrences
- `chunk_id` references `chunks` — provenance (which chunk produced this edge)
- `supporting_chunks` UUID[] — running list of all chunks that contributed

**`entity_mentions`** — one row per (entity, chunk, character offset). Lets the graph link back to exact spans in the source text.
- `id`, `entity_id`, `chunk_id`, `start_offset`, `end_offset`, `created_at`

**`query_log`** — observability table for search latency and result counts. Used by the Stats dashboard.

### Why one database, not two

Vector embeddings, full-text indexes, knowledge graph, tokens, and observability all live in the same Postgres. No separate vector DB to keep in sync, no second backup story, one connection string. HNSW indexes plus tuned `maintenance_work_mem` (1 GB default in the bundled `docker-compose.yml`) keep search fast at the scale a self-hosted personal-memory tool actually runs at.

---

## Embeddings

**Model:** `all-MiniLM-L6-v2` from sentence-transformers — 384 dimensions, runs on CPU, no GPU required, no API calls.

The model is downloaded on first ingest and cached under `~/.cache/huggingface`. In Docker, this is part of the image build, so the running container has zero first-run download.

**Index:** pgvector HNSW with `vector_cosine_ops`. Default `ef_search=40` — fine at thousands of chunks, starts to lose recall past tens of thousands.

---

## Hybrid Search

The core differentiator. Pure vector search misses exact terms (model names, error codes, file paths); pure keyword search misses paraphrase. Memory Vault runs both and merges them with **Reciprocal Rank Fusion**.

### Algorithm

```
score(d) = Σ_{r ∈ rankers} 1 / (k + rank_r(d))
```

with `k = 60` (Cormack et al. 2009 default). Higher score wins. Documents found by both rankers score higher than documents found by only one. RRF uses ranks, not raw scores — no normalization needed between the cosine-similarity-of-vectors world and the `ts_rank`-of-tsvector world.

### Implementation

For each query:
1. **Query enrichment** — up to 3 query variations are generated using the embedding model's WordPiece tokenizer to extract key technical terms. Improves recall without losing precision.
2. **Vector branch** — embed the query, run HNSW similarity search, take top 50.
3. **Keyword branch** — `plainto_tsquery` against `content_tsv`, take top 50 by `ts_rank_cd`.
4. **RRF merge** — `UNION ALL` both ranked lists, sum `1/(60+rank)` per chunk, return top K (default K=20).

The same engine powers the REST `/api/search`, MCP `recall`, and the dashboard's Search and Chat pages — there's no second retrieval implementation.

---

## Ingestion Pipeline

Async pipeline. Adapters convert different input formats into a list of `RawChunk` objects, then a single ingestion service handles dedup, embedding, and storage.

### Adapters (v1.0)

Located in `src/adapters/`:

- **`markdown.py`** — splits by headings, preserves structure
- **`plaintext.py`** — paragraph-based with smart merging
- **`claude.py`** — parses Claude conversation exports

Each adapter returns `RawChunk(text, speaker, timestamp, chunk_index, metadata, content_hash)`. New adapters (PDF, web pages) are part of the PRO tier.

### Pipeline stages

For every chunk produced by an adapter:

1. **Parse → chunk** — adapter-specific.
2. **Dedup check** — `content_hash` (SHA-256) is compared against existing chunks in the target space. Duplicates are skipped, not re-embedded.
3. **Embed** — sentence-transformers, batched (`EMBEDDING_BATCH_SIZE=32` by default).
4. **Insert** — single `INSERT` per chunk; `content_tsv` is populated by a Postgres trigger automatically.
5. **Extract entities** — spaCy NER + multi-token noun-phrase detection runs synchronously on the same CPU as the embedding step. Entities, relationships, and entity_mentions are written in the same transaction as the chunk.

If extraction fails for any reason, the chunk is still committed — extraction is best-effort, not a blocker for storage.

---

## Knowledge Graph

Built without an LLM. Every chunk passes through spaCy at ingest time, and entities + relationships flow into the graph automatically.

### Extraction

- **spaCy NER** with `en_core_web_sm` (~15 MB, CPU-only) tags `PERSON`, `ORG`, `PRODUCT`, mapped to **person**, **project**, **tool** entity types respectively.
- **Concept extraction** — multi-token noun phrases that appear at least twice within a single chunk become **concept** entities.
- **Per-space dedup** — entities are unique by `(lower(name), type, space_id)`. The same name in two different spaces is two different entities; the same name with two different types in the same space is also two entities.

### Relationships

- **Co-occurrence** — any two entities found in the same chunk produce a `related_to` edge in the relationships table, with `chunk_id` recording which chunk produced it.
- **Strength** — edge weight increments on every co-occurrence across chunks. Frequent co-occurrence → higher strength → larger edge in the visualization.

### Visualization

- **`/api/graph/entities`** and **`/api/graph/relationships`** — paginated raw access for any client.
- **`/api/graph/visualize`** — bundled nodes + edges payload sized for the dashboard, with truncation rules (max 500 nodes by default, configurable per request).
- **Dashboard Graph page** — Cytoscape.js force-directed layout (`cose-bilkent`), pan/zoom, click-through to entity detail (mentions across chunks, related entities).

### Why no LLM

The default move in this space is to feed every chunk through an LLM and ask for entities and relationships. It works but couples graph quality to whichever model you happened to pick, and costs money on every ingest. spaCy + co-occurrence gets ~80% of the way for 0% of the LLM cost, with documented honest limits (English-only NER, context-dependent entity typing, no fuzzy matching).

---

## REST API

FastAPI with bearer auth, rate limiting, and an auto-generated OpenAPI page at `/docs`. Served at `http://localhost:8000` after `docker compose up`.

### Endpoints (v1.0)

| Method | Path | Description |
|---|---|---|
| `GET` | `/api/health` | Service + DB health (no auth) |
| `GET` | `/api/spaces` | List memory spaces with chunk counts |
| `POST` | `/api/spaces` | Create a new memory space |
| `GET` | `/api/chunks` | List chunks with pagination + filters |
| `GET` | `/api/chunks/{id}` | Get a single chunk |
| `DELETE` | `/api/chunks/{id}` | Soft-delete a chunk |
| `POST` | `/api/search` | Hybrid search |
| `POST` | `/api/ingest/text` | Ingest a text string |
| `POST` | `/api/ingest/file` | Upload a file through the ingestion pipeline |
| `POST` | `/api/chat` | RAG chat (non-streaming) |
| `POST` | `/api/chat/stream` | RAG chat with token-by-token SSE streaming |
| `GET` | `/api/graph/entities` | List entities with filters |
| `GET` | `/api/graph/entities/{id}` | Entity detail (mentions, related) |
| `GET` | `/api/graph/relationships` | List edges with filters |
| `GET` | `/api/graph/visualize` | Node + edge payload sized for visualization |

### Authentication

All endpoints except `/api/health` require a bearer token. Tokens are created via the CLI (`memory-vault token create <name>`) and shown once in plaintext. Stored as SHA-256 hashes; lookup is constant-time.

To disable auth entirely (local dev only): `API_AUTH_ENABLED=false`.

### Rate limiting

Simple per-IP request counter, default 120 req/min. Returns `429 Too Many Requests` with `Retry-After` header when tripped. Configurable via `API_RATE_LIMIT_PER_MIN`.

### Error handling

Global exception handler returns generic 500 with no traceback. `psycopg.OperationalError` returns generic 503. Full traces go to structured JSON logs only, correlated by an `X-Request-ID` header that's emitted on every response.

---

## MCP Server

Memory Vault's MCP server exposes Memory Vault to Claude Desktop and Claude Code via the [Model Context Protocol](https://modelcontextprotocol.io/). Transport is **stdio** — no HTTP, no ports.

### Tools

| Tool | Signature |
|---|---|
| `recall` | `(query: str, space: str = None, limit: int = 10)` — hybrid search |
| `remember` | `(text: str, space: str = "default", source: str = None, speaker: str = None)` — ingest a chunk |
| `forget` | `(chunk_id: str)` — soft-delete |
| `memory_status` | `()` — DB health, chunk count, spaces breakdown |

### Resources

| Resource | Returns |
|---|---|
| `memory://spaces` | List of spaces with chunk counts |
| `memory://stats` | System statistics |

### Config

For Claude Code, in your project's `.mcp.json`:

```json
{
  "mcpServers": {
    "memory-vault": {
      "command": "python",
      "args": ["-m", "src.mcp"],
      "cwd": "/path/to/memory-vault",
      "env": {
        "PYTHONPATH": "/path/to/memory-vault",
        "DB_HOST": "localhost",
        "DB_PORT": "5432",
        "DB_NAME": "memory_vault",
        "DB_USER": "memory_vault",
        "DB_PASSWORD": "memory_vault"
      }
    }
  }
}
```

Same JSON works in Claude Desktop's `Settings → Developer → Edit Config`.

The MCP server is not a wrapper around the REST API — both are first-class entry points into the same service code (search engine, ingestion pipeline, graph extraction).

See the [README](README.md#mcp-integration-claude-desktop--claude-code) for setup details, including the Docker-host configuration variant.

---

## Web Dashboard

React 19 + Vite + TanStack Query, built into a static bundle at image build time and served by FastAPI from `src/api/static/`. No separate process, no separate port, no `npm start` in production.

### Pages (v1.0)

- **Chat** (default landing page) — talk to the vault with a local LLM, sources panel shows the retrieved chunks for every answer
- **Search** — hybrid search with space filter, similarity scores, expandable hit content
- **Browse** — paginated chunk list, space + sort filters, two-step inline delete
- **Graph** — Cytoscape force-directed knowledge graph, pan/zoom, type/min-mentions/max-nodes filters
- **Ingest** — paste text or upload files (one at a time in v1.0), per-file status, batch summary
- **Stats** — total chunks, spaces table with visual distribution, auto-refresh every 30s

### Auth

Same bearer token as the REST API. The dashboard prompts on first load, stores the token in `localStorage` under `memory-vault-token`, and auto-clears on a 401 response.

---

## Local LLM Chat

The dashboard's Chat page lets you talk to your own memories through a local LLM — no cloud, no API keys, no telemetry.

**v1.0 supports LM Studio** as the local LLM provider. Ollama and llama.cpp use the same OpenAI-compatible client architecture under the hood, but are not supported in v1.0.

### Flow

1. User types a question on the Chat page.
2. The dashboard calls `POST /api/chat/stream` with the question + selected space + retrieval limit.
3. The chat router runs hybrid search against the vault and packs the top hits into a context block sized for a 6,000-token budget (oldest history dropped first, then lowest-similarity chunks).
4. The router calls LM Studio's local API with `{system, context, user-question}`.
5. Sources are sent to the dashboard **first** in the SSE stream, so the UI can render "based on N memories" before tokens start flowing.
6. The LLM answer streams back token-by-token.

### Why LM Studio first

LM Studio's native API supports `reasoning="off"`, which is the only reliable way to suppress chain-of-thought from thinking-capable models in a RAG flow. Memory Vault uses the native API by default and falls back to OpenAI-compat (`/v1/chat/completions`) with `<think>...</think>` stripping if the native API isn't available.

---

## CLI

A `memory-vault` command-line tool ships in the same Docker image as the API. Used for migrations, status checks, ingestion, search, token management, space management, and the diagnostic bundler.

```bash
memory-vault status              # DB health, chunk count, spaces
memory-vault ingest path/to/file # one-shot ingest, with --space flag
memory-vault search "query"      # CLI search with verbose output
memory-vault token create my-app # create a bearer token
memory-vault token list / revoke
memory-vault space create / list
memory-vault migrate             # run pending migrations (idempotent)
memory-vault diagnose            # produce a redacted diagnostic zip
memory-vault api                 # start the REST API (uvicorn)
memory-vault mcp                 # start the MCP server (stdio)
```

The diagnostic bundler is the entry point for bug reports — see [SECURITY.md](SECURITY.md) and [CONTRIBUTING.md](CONTRIBUTING.md).

---

## Docker Setup

The bundled `docker-compose.yml` defines two services and runs everything with one command.

```yaml
services:
  db:
    image: pgvector/pgvector:pg16
    command:
      - postgres
      - -c
      - maintenance_work_mem=1GB
    volumes:
      - pgdata:/var/lib/postgresql/data

  app:
    build: .
    image: memory-vault-app:latest
    ports: ["8000:8000"]
    depends_on:
      db: { condition: service_healthy }
```

The image is **multi-stage**: stage 1 is a Node 20 builder that runs `npm run build` to produce the dashboard bundle; stage 2 is a Python 3.11 runtime that pip-installs the package, downloads the spaCy `en_core_web_sm` model, and copies the dashboard bundle into `src/api/static/`.

Released images are published to **`ghcr.io/mihaibuilds/memory-vault`** for both `linux/amd64` and `linux/arm64` (Apple Silicon, Raspberry Pi 4+, ARM VPSs). The release workflow (`.github/workflows/release.yml`) handles the multi-arch build on every `v*.*.*` tag.

First run: migrations apply automatically (`memory-vault migrate` is idempotent), the `default` memory space is created, the spaCy model is already in the image, and `http://localhost:8000` serves the dashboard.

---

## Design Decisions

**Why PostgreSQL + pgvector instead of a dedicated vector database (Pinecone, Weaviate, Qdrant)?**
One database means one backup story, one connection string, one operational mental model. For a single-tenant self-hosted personal-memory tool, the marginal performance of a dedicated vector store doesn't outweigh the operational cost of running and syncing two systems. When that stops being true, the migration path to a hybrid setup is sane.

**Why hybrid search instead of vector-only?**
Pure vector search is great at paraphrase and concept; bad at exact strings. Pure keyword search is the opposite. RRF gets both with no parameter tuning.

**Why `all-MiniLM-L6-v2` instead of a larger embedding model?**
384 dimensions runs fast on CPU, fits in RAM on any modern laptop, and quality is good enough for personal memory. The goal is "anyone can run this," not maximum benchmark performance.

**Why spaCy + co-occurrence for the knowledge graph instead of an LLM?**
LLM extraction couples graph quality to whichever model you happened to pick and bills you on every ingest. spaCy is fast, free, deterministic, and the limitations are documented honestly up front. Manual entity merging in the dashboard is not in v1.0.

**Why MCP-first, not REST-first?**
Memory Vault was designed around the assumption that the primary user of this database is going to be Claude, not me. The REST API exposes the same operations for human-driven apps, but the MCP server isn't a wrapper around the REST API — both are direct paths into the same service code.

**Why MIT-licensed core?**
Your AI memory should belong to you, not a cloud platform. The free tier is the full personal-memory product. The PRO tier adds operational and scale features that teams pay for — multi-user activation, dedup with importance decay, encrypted backups, conflict resolution, additional adapters.

---

## What's in v1.0

Hybrid search, MCP, knowledge graph (no LLM), REST API, dashboard with Chat/Search/Browse/Graph/Ingest/Stats, local LLM chat (LM Studio), one-command Docker, multi-arch images, MIT-licensed.

For honest v1.0 limitations, see the [Limitations section in the README](README.md#limitations).
