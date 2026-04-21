# Memory Vault

**A local-first AI memory system with hybrid search, MCP integration, and a knowledge graph.**

Every conversation with Claude or ChatGPT starts from zero. No memory of what you built last week, what decisions you made last month, what problems you've already solved. You either re-explain everything from scratch, or paste in a wall of context and hope it fits in the window.

Memory Vault fixes that. It stores everything you want your AI to remember — decisions, conversations, notes, project context — and makes it searchable through hybrid semantic + keyword search. Claude can recall and store memories during any conversation through MCP, without you doing anything manually.

---

## Features

- **Hybrid search** — semantic similarity + keyword matching combined, so you find the right memory even when you don't remember the exact words
- **MCP integration** — four tools (`recall`, `remember`, `forget`, `memory_status`) that Claude can use natively during any session
- **Knowledge graph** — entities and relationships extracted automatically, connections between things emerge over time
- **Memory spaces** — separate namespaces for different projects or domains
- **Local LLM chat** — query your own memories through Ollama or LM Studio without sending anything to the cloud
- **REST API** — integrate AI memory into any application
- **One-command setup** — `docker compose up` and it's running
- **Self-hosted** — your data stays on your machine, always

---

## Status

**This project is being built in public.** Follow the build:

| Milestone | Status | Description |
|-----------|--------|-------------|
| M1 — The Announcement | ✅ Done | README, architecture overview, project vision |
| M2 — The Core | ✅ Done | Hybrid search engine, ingestion pipeline, embeddings |
| M3 — One Command to Start | ✅ Done | Docker setup, `docker compose up` and it works |
| M4 — Talk to Claude | ✅ Done | MCP server — Claude reads and writes memories mid-conversation |
| M5 — The API | ✅ Done | REST API with bearer auth, rate limiting, OpenAPI docs |
| M6 — The Dashboard | ✅ Done | Web UI for search, browse, ingest, stats |
| M7 — The Knowledge Graph | ⏳ Planned | Entity extraction and visualization |
| M8 — v1.0 Release | ⏳ Planned | Local LLM chat, polish, full launch |
| M9 — PRO Unlocked | ⏳ Planned | Team features, advanced analytics, paid tier |

Each milestone is a working, usable increment — not a placeholder, not a demo.

---

## Quick Start (Docker)

```bash
git clone https://github.com/MihaiBuilds/memory-vault.git
cd memory-vault
docker compose up -d
```

That's it. PostgreSQL + pgvector + Memory Vault, running and ready. Migrations run automatically on first start.

```bash
# Check it's working
docker compose exec app memory-vault status

# Ingest a file
docker compose exec app memory-vault ingest /path/to/file.md --space default

# Search
docker compose exec app memory-vault search "your query here"
```

Data persists in a Docker volume — `docker compose down` and `up` again, your memories are still there.

---

## Windows users — read this first

If you're on Windows and you see this error when starting the container:

```
exec ./scripts/start.sh: no such file or directory
```

(and the container exits with code 255), it's a line-ending issue, not a missing file. Windows git installs default to `core.autocrlf=true`, which can rewrite `scripts/start.sh` with `\r\n` line endings. Linux then reads the shebang as `#!/bin/sh\r` and tries to run an interpreter literally named `sh\r`.

Memory Vault now ships with defensive `.gitattributes` rules and strips carriage returns inside the Docker image, so fresh clones should just work. If you cloned before this fix, or you still hit the error, run this **inside the repo** — do NOT change your global git config, it will break your other Windows projects:

```bash
cd memory-vault
git config core.autocrlf false
git rm --cached -r .
git reset --hard
docker compose build --no-cache
docker compose up -d
```

`--no-cache` matters because Docker caches the broken version in a build layer — rebuilding without it won't fix the issue.

**Performance tip:** clone Memory Vault into your WSL2 filesystem (for example `~/memory-vault`), not a Windows path (`C:\Users\...`). Docker Desktop on Windows pays a significant I/O cost crossing between the Windows filesystem and the Linux VM, and search latency can drop from seconds to milliseconds when the repo lives inside WSL2.

---

## Installation (Manual)

If you prefer running without Docker:

### Prerequisites

- Python 3.11+
- PostgreSQL 16 with [pgvector](https://github.com/pgvector/pgvector) extension

### Setup

```bash
# Clone
git clone https://github.com/MihaiBuilds/memory-vault.git
cd memory-vault

# Create virtual environment
python -m venv .venv
source .venv/bin/activate

# Install dependencies
pip install -e .

# Configure
cp .env.example .env
# Edit .env with your PostgreSQL credentials

# Run migrations
memory-vault migrate

# Verify
memory-vault status
```

### Usage

```bash
# Ingest a file
memory-vault ingest notes.md --space default

# Search memories
memory-vault search "hybrid search architecture" --limit 5

# Check status
memory-vault status
```

---

## MCP Integration (Claude Desktop & Claude Code)

Memory Vault exposes four tools via the [Model Context Protocol](https://modelcontextprotocol.io/) so Claude can read and write memories during any conversation.

### Tools

| Tool | Description |
|------|-------------|
| `recall` | Search memories with hybrid search (vector + full-text + RRF) |
| `remember` | Store a new memory — auto-classified and embedded |
| `forget` | Soft-delete a memory by chunk ID |
| `memory_status` | Database health, chunk counts, embedding model info |

### Resources

| Resource | Description |
|----------|-------------|
| `memory://spaces` | List all memory spaces with chunk counts |
| `memory://stats` | Current system statistics |

### Setup — Claude Code

Add to your project's `.mcp.json`:

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

### Setup — Claude Desktop

Add the same config to Claude Desktop's settings (`Settings → Developer → Edit Config`). The server runs over stdio — no HTTP, no ports to expose.

### Docker Users

If you're running Memory Vault via Docker, point `DB_HOST` at the Docker host:

```json
{
  "mcpServers": {
    "memory-vault": {
      "command": "python",
      "args": ["-m", "src.mcp"],
      "cwd": "/path/to/memory-vault",
      "env": {
        "PYTHONPATH": "/path/to/memory-vault",
        "DB_HOST": "127.0.0.1",
        "DB_PORT": "5432",
        "DB_NAME": "memory_vault",
        "DB_USER": "memory_vault",
        "DB_PASSWORD": "memory_vault"
      }
    }
  }
}
```

> The MCP server itself runs on the host (not inside Docker) and connects to the PostgreSQL container. Make sure port 5432 is exposed in your `docker-compose.yml`.

### Verify It Works

Once configured, Claude will have access to the memory tools. Try:

> "Use memory_status to check the memory system."

> "Remember that we decided to use PostgreSQL for all storage."

> "Recall everything about hybrid search."

---

## REST API

Every MCP tool is also exposed as an HTTP endpoint so you can integrate Memory Vault into any app, script, or language. The API is served by FastAPI at `http://localhost:8000` when you run `docker compose up`.

- **Interactive docs:** http://localhost:8000/docs
- **OpenAPI schema:** http://localhost:8000/openapi.json

### Authentication

All endpoints except `/api/health` require a bearer token. Create one via the CLI:

```bash
docker compose exec app memory-vault token create my-app
```

The plaintext token is shown **once** — copy it immediately. Then send it as a header:

```bash
curl -H "Authorization: Bearer mv_..." http://localhost:8000/api/spaces
```

Manage tokens:

```bash
memory-vault token list
memory-vault token revoke mv_abc1234
```

To disable auth entirely (local dev only), set `API_AUTH_ENABLED=false`.

### Endpoints

| Method | Path | Description |
|--------|------|-------------|
| `GET`    | `/api/health`           | Service + database health (no auth) |
| `GET`    | `/api/spaces`           | List memory spaces with chunk counts |
| `POST`   | `/api/search`           | Hybrid search (vector + full-text + RRF) |
| `GET`    | `/api/chunks`           | List chunks with pagination and filters |
| `GET`    | `/api/chunks/{id}`      | Get a single chunk |
| `DELETE` | `/api/chunks/{id}`      | Soft-delete (forget) a chunk |
| `POST`   | `/api/ingest/text`      | Ingest a text string as a chunk |
| `POST`   | `/api/ingest/file`      | Upload a file through the ingestion pipeline |

### Example — search

```bash
curl -X POST http://localhost:8000/api/search \
  -H "Authorization: Bearer $MV_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "query": "how does hybrid search work",
    "spaces": ["default"],
    "limit": 5
  }'
```

### Example — ingest text

```bash
curl -X POST http://localhost:8000/api/ingest/text \
  -H "Authorization: Bearer $MV_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"text": "Decided to use RRF for hybrid merging", "space": "default"}'
```

### Example — upload a file

```bash
curl -X POST http://localhost:8000/api/ingest/file \
  -H "Authorization: Bearer $MV_TOKEN" \
  -F "file=@notes.md" \
  -F "space=default"
```

### Configuration

| Variable | Default | Description |
|----------|---------|-------------|
| `API_HOST` | `0.0.0.0` | Bind address |
| `API_PORT` | `8000` | Port |
| `API_AUTH_ENABLED` | `true` | Set `false` to disable bearer auth (local dev only) |
| `API_CORS_ORIGINS` | `*` | Comma-separated allowed origins, or `*` |
| `API_RATE_LIMIT_PER_MIN` | `120` | Per-IP request limit per minute |

---

## Dashboard

Memory Vault ships with a web UI baked into the same Docker image as the API — no separate deploy, no extra port. Open `http://localhost:8000` in your browser after `docker compose up`.

Four pages:

- **Search** — hybrid search with space filter, similarity scores, expandable hit content
- **Browse** — paginated chunk list with space + sort filters, two-step inline delete
- **Ingest** — paste text or upload files (one at a time in v1.0), per-file status, batch summary
- **Stats** — system health, total chunks, spaces table with visual distribution, auto-refresh every 30s

### Access

The dashboard uses the same bearer token as the API. Create one and paste it into the dashboard's token screen:

```bash
docker compose exec app memory-vault token create dashboard
```

The plaintext token is shown **once** — copy it immediately. Open `http://localhost:8000`, paste into the prompt, and the dashboard stores it in `localStorage` under `memory-vault-token`. You won't be asked again on that browser.

### Rotating or revoking

```bash
# See which tokens exist
docker compose exec app memory-vault token list

# Revoke by prefix (shown in list output)
docker compose exec app memory-vault token revoke mv_abc1234

# Create a new one
docker compose exec app memory-vault token create dashboard
```

After revoking, the dashboard will hit a 401 on its next request and auto-clear the stored token, forcing you to paste the new one.

### Troubleshooting

- **Prompted for token every reload:** your browser is blocking `localStorage` (private mode, strict cookie settings). Use a normal window or allow storage for `localhost`.
- **401 on every request:** the token was revoked or `API_AUTH_ENABLED` changed. Create a fresh token and paste it in.
- **Dashboard shows but API calls fail with CORS:** you're hitting the API on a different origin than the dashboard. The baked-in build avoids this — use `http://localhost:8000`, not the dev server, unless you know what you're doing.
- **Running the dev server:** `cd web && npm install && npm run dev` serves the UI at `http://localhost:5173` with API calls proxied to `:8000`. For development only.

---

## How It Works

### Hybrid Search

Memory Vault combines two search methods and merges the results:

1. **Vector search** — converts your query to an embedding, finds semantically similar chunks via HNSW index
2. **Full-text search** — keyword matching via PostgreSQL tsvector + GIN index
3. **RRF merging** — Reciprocal Rank Fusion combines both ranked lists so neither method dominates

This means you find the right memory whether you remember the exact words or just the concept.

### Query Enrichment

Before searching, Memory Vault generates up to 3 query variations using the embedding model's WordPiece tokenizer to extract key technical terms. This improves recall without losing precision.

### Ingestion Pipeline

Async queue-based pipeline with adapters for different input formats:

- **Markdown** — splits by headings, preserves structure
- **Plain text** — paragraph-based with smart merging
- **Claude JSON** — parses Claude conversation exports

---

## Tech Stack

- **PostgreSQL 16 + pgvector** — vector storage and hybrid search in one database
- **Python 3.11+** — async backend with psycopg 3
- **sentence-transformers** — `all-MiniLM-L6-v2` embeddings (384-d, runs on CPU)
- **FastAPI** — REST API with bearer auth, rate limiting, and OpenAPI docs
- **React 19 + Vite + TanStack Query** — web dashboard, baked into the main Docker image
- **Docker** — one-command deployment with `docker compose up`
- **MCP** — Claude integration via FastMCP (stdio transport)

---

## License

The core is **MIT licensed** — free forever. Everything that makes Memory Vault useful as a personal memory system (hybrid search, MCP integration, knowledge graph, dashboard, local LLM chat, Docker setup) will always be free and open source.

A PRO tier for teams and advanced features is planned for Milestone 9.

---

## Follow the Build

- Website: [mihaibuilds.com](https://mihaibuilds.com)
- Blog: [mihaibuilds.com/blog](https://mihaibuilds.com/blog)
- GitHub: [@MihaiBuilds](https://github.com/MihaiBuilds)
- X: [@mihaibuilds](https://x.com/mihaibuilds)

> Watch the repo to follow along as each milestone ships.
