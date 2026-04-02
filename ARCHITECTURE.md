# Memory Vault — Architecture Overview

This document describes the technical architecture of Memory Vault. It covers the core components, how they connect, and the key design decisions behind them.

---

## High-Level Architecture

```
┌─────────────────────────────────────────────────────────────┐
│                        Clients                              │
│                                                             │
│   Claude (MCP)    Web Dashboard    REST API    CLI          │
└────────┬──────────────┬───────────────┬──────────┬──────────┘
         │              │               │          │
         └──────────────┴───────────────┴──────────┘
                                │
                    ┌───────────▼───────────┐
                    │      FastAPI App       │
                    │   (API + MCP Server)   │
                    └───────────┬───────────┘
                                │
              ┌─────────────────┼─────────────────┐
              │                 │                 │
    ┌─────────▼──────┐ ┌───────▼────────┐ ┌──────▼──────────┐
    │ Ingestion      │ │ Search Engine  │ │ Knowledge Graph │
    │ Pipeline       │ │                │ │                 │
    │ - Markdown     │ │ - Vector (HNSW)│ │ - Entity extrac │
    │ - Plaintext    │ │ - Full-text    │ │ - Relationships │
    │ - Claude JSON  │ │ - RRF merging  │ │ - Graph queries │
    └─────────┬──────┘ └───────┬────────┘ └──────┬──────────┘
              │                │                 │
              └────────────────┴─────────────────┘
                                │
                    ┌───────────▼───────────┐
                    │     PostgreSQL         │
                    │   + pgvector           │
                    │                        │
                    │  chunks table          │
                    │  entities table        │
                    │  relationships table   │
                    └───────────────────────┘
```

---

## Core Components

### 1. Storage — PostgreSQL + pgvector

The foundation. Everything is stored in PostgreSQL with the `pgvector` extension for vector similarity search.

**`chunks` table** — the primary storage unit:
- `id` — UUID primary key
- `content` — raw text of the memory
- `embedding` — 384-dimension vector (`all-MiniLM-L6-v2`)
- `tsv` — tsvector column for full-text search (GIN index)
- `space` — namespace (e.g. `work`, `learning`, `personal`)
- `source` — where it came from (file path, URL, conversation)
- `created_at`, `updated_at`

**`entities` table** — extracted knowledge graph nodes:
- `id`, `name`, `type` (person, project, tool, concept), `space`

**`relationships` table** — knowledge graph edges:
- `from_entity_id`, `to_entity_id`, `relationship_type` (works_on, uses, depends_on)

---

### 2. Embeddings

Model: `all-MiniLM-L6-v2` (sentence-transformers)
- 384 dimensions — small, fast, runs on CPU, good quality
- Runs locally — no API calls, no data leaving the machine
- Batch embedding for ingestion performance

Index: HNSW (Hierarchical Navigable Small World) via pgvector
- Fast approximate nearest-neighbor search
- Much faster than exact search at scale, minimal accuracy loss

---

### 3. Hybrid Search Engine

The core differentiator. Combines two search methods and merges results:

**Vector search** — semantic similarity
- Converts the query to an embedding
- Finds chunks with closest vectors via HNSW index
- Finds conceptually similar content even with different words

**Full-text search** — keyword matching
- Uses PostgreSQL `tsvector` + `tsquery`
- GIN index for fast keyword lookup
- Finds exact word matches the vector search might miss

**RRF merging (Reciprocal Rank Fusion)**
- Merges the two ranked lists into one
- Formula: `score = Σ 1 / (k + rank)` where k=60
- Neither method dominates — both contribute
- Result: better recall than either method alone

---

### 4. Ingestion Pipeline

Async queue-based pipeline. Adapters convert different input formats into chunks:

- **Markdown adapter** — splits by headers, preserves structure
- **Plaintext adapter** — sliding window with overlap
- **Claude JSON adapter** — parses Claude Code memory format (the format this system was built on)

Each chunk goes through: parse → chunk → embed → store.

---

### 5. MCP Server

Connects Memory Vault to Claude via the Model Context Protocol.

**Transport:** stdio (Claude Desktop and Claude Code compatible)

**Tools:**
- `recall(query, space?, limit?)` — hybrid search, returns top-N memories
- `remember(text, space, source?)` — ingest a new memory
- `forget(chunk_id)` — mark a memory for removal
- `memory_status(space?)` — stats: chunk count, space breakdown, last ingest

**Resources:**
- `memory://spaces` — list of all memory spaces
- `memory://stats` — system statistics

**Config (`.mcp.json`):**
```json
{
  "mcpServers": {
    "memory-vault": {
      "command": "docker",
      "args": ["exec", "-i", "memory-vault", "python", "-m", "src.mcp_server"],
      "env": {}
    }
  }
}
```

---

### 6. REST API

FastAPI-based. All functionality is accessible via HTTP for integrations and custom clients.

**Key endpoints:**
- `POST /api/search` — hybrid search
- `POST /api/ingest` — ingest text or file
- `GET /api/chunks` — browse stored memories
- `GET /api/spaces` — list memory spaces
- `DELETE /api/chunks/{id}` — remove a memory
- `GET /api/graph/entities` — knowledge graph entities
- `GET /api/graph/visualize` — graph visualization data
- `GET /health` — system health check

Authentication: bearer token. Token generated at first run, printed to console.

Auto-generated OpenAPI docs at `/docs`.

---

### 7. Web Dashboard

React app bundled into FastAPI static serving — no separate process, no `npm start`.

Pages:
- **Search** — search bar, results with similarity scores and source provenance
- **Browse** — all memories, filter by space, sort by date/importance
- **Ingest** — drag-and-drop upload, paste text, select adapter
- **Graph** — interactive force-directed knowledge graph visualization
- **Stats** — chunk counts, space breakdown, query volume, system health

Accessible at `http://localhost:8000` after `docker compose up`.

---

### 8. Local LLM Chat

Query your memories through a local language model — no cloud, no API keys.

Integrations:
- **Ollama** — `http://localhost:11434`
- **LM Studio** — `http://localhost:1234`

Flow: question → hybrid search → top-N chunks as context → local LLM generates answer → response with source citations.

Your data never leaves your machine.

---

## Docker Setup

Two containers, one compose file:

```yaml
services:
  db:
    image: pgvector/pgvector:pg16
    volumes:
      - postgres_data:/var/lib/postgresql/data

  memory-vault:
    image: mihaibuilds/memory-vault:latest
    ports:
      - "8000:8000"
    depends_on:
      - db
    environment:
      - DATABASE_URL=postgresql://...
```

First run: auto-detects empty DB, runs migrations, creates default space, generates API token.

---

## Design Decisions

**Why PostgreSQL instead of a dedicated vector database (Pinecone, Weaviate, Qdrant)?**
PostgreSQL with pgvector handles hybrid search (vector + full-text) in a single query. Dedicated vector databases require a separate full-text search system alongside them. One database, one backup, one connection string.

**Why `all-MiniLM-L6-v2` instead of a larger model?**
384 dimensions runs fast on CPU, fits in RAM on any machine, and quality is good enough for personal knowledge. The goal is a system anyone can run, not maximum benchmark performance.

**Why RRF over a weighted score merge?**
RRF requires no tuning — there's no weight parameter to get wrong. It consistently outperforms naive score averaging and works well across very different query types.

**Why MIT license for the core?**
Your AI memory should belong to you, not a cloud platform. The free version being genuinely useful (not crippled) is what builds the community and trust. PRO features are operational/scale features that teams pay for — not capabilities withheld from individual users.

---

## What's Not Built Yet

This document describes the target architecture. The code ships starting in Milestone 2.

Follow the build at [mihaibuilds.com/blog](https://mihaibuilds.com/blog).
