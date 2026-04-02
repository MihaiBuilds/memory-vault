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

**This project is being built in public.** The repository is live but the code ships in Milestone 2. Follow the build:

| Milestone | Status | Description |
|-----------|--------|-------------|
| M1 — The Announcement | ✅ Live | This README, architecture overview, project vision |
| M2 — The Core | 🔜 Next | Hybrid search engine, ingestion pipeline, embeddings |
| M3 — One Command to Start | ⏳ Planned | Docker setup, `docker compose up` and it works |
| M4 — Talk to Claude | ⏳ Planned | MCP server with full tool support |
| M5 — The Dashboard | ⏳ Planned | Web UI for search, browse, ingest |
| M6 — The REST API | ⏳ Planned | For integrations and custom clients |
| M7 — The Knowledge Graph | ⏳ Planned | Entity extraction and visualization |
| M8 — v1.0 Release | ⏳ Planned | Local LLM chat, polish, full launch |
| M9 — PRO Unlocked | ⏳ Planned | Team features, advanced analytics, paid tier |

Each milestone is a working, usable increment — not a placeholder, not a demo.

---

## Tech Stack

- **PostgreSQL + pgvector** — vector storage and hybrid search
- **Python** — backend, ingestion pipeline, MCP server
- **FastAPI** — REST API and dashboard serving
- **React** — web dashboard
- **Docker** — one-command self-hosted deployment
- **MCP (Model Context Protocol)** — Claude integration

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

> Watch the repo to follow along. The build starts now.
