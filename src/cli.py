"""
CLI entrypoint — memory-vault command with subcommands.

Usage:
    memory-vault ingest <file> [--space default]
    memory-vault search <query> [--space default] [--limit 5]
    memory-vault status
    memory-vault migrate
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="memory-vault",
        description="Memory Vault — local AI memory system",
    )
    sub = parser.add_subparsers(dest="command")

    # ingest
    p_ingest = sub.add_parser("ingest", help="Ingest a file into memory")
    p_ingest.add_argument("file", help="Path to file to ingest")
    p_ingest.add_argument("--space", default="default", help="Memory space name")

    # search
    p_search = sub.add_parser("search", help="Search memories")
    p_search.add_argument("query", help="Search query")
    p_search.add_argument("--space", default=None, help="Filter by space name")
    p_search.add_argument("--limit", type=int, default=5, help="Max results")

    # status
    sub.add_parser("status", help="Show system status")

    # migrate
    sub.add_parser("migrate", help="Run database migrations")

    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        sys.exit(1)

    if args.command == "migrate":
        asyncio.run(_cmd_migrate())
    elif args.command == "ingest":
        asyncio.run(_cmd_ingest(args.file, args.space))
    elif args.command == "search":
        asyncio.run(_cmd_search(args.query, args.space, args.limit))
    elif args.command == "status":
        asyncio.run(_cmd_status())


async def _cmd_migrate() -> None:
    from src.models.db import init_pool, run_migrations, close_pool

    await init_pool()
    await run_migrations()
    await close_pool()
    print("Migrations complete.")


async def _cmd_ingest(file_path: str, space: str) -> None:
    from pathlib import Path
    from src.models.db import init_pool, fetch_one, close_pool
    from src.services.ingestion import IngestionPipeline

    path = Path(file_path)
    if not path.exists():
        print(f"File not found: {file_path}")
        sys.exit(1)

    await init_pool()

    row = await fetch_one(
        "SELECT id FROM memory_spaces WHERE name = %s", (space,)
    )
    if not row:
        print(f"Unknown space: {space}")
        await close_pool()
        sys.exit(1)

    space_id = row["id"]
    pipeline = IngestionPipeline(max_workers=1)
    pipeline.enqueue(str(path.resolve()), space_id)
    stats = await pipeline.run_all()

    await close_pool()
    print(f"Ingested: {stats.chunks_created} chunks created, "
          f"{stats.failed} failed")


async def _cmd_search(query: str, space: str | None, limit: int) -> None:
    from src.models.db import init_pool, close_pool
    from src.services.search import hybrid_search, resolve_space_names

    await init_pool()

    space_ids = await resolve_space_names([space] if space else None)
    results, variations, elapsed_ms = await hybrid_search(
        query, space_ids=space_ids or None, limit=limit,
    )

    await close_pool()

    print(f"\nSearch: \"{query}\"")
    print(f"Variations: {variations}")
    print(f"Results: {len(results)} ({elapsed_ms}ms)\n")

    for i, r in enumerate(results, 1):
        print(f"  [{i}] {r.similarity:.4f}  [{r.space}]  {r.source or 'unknown'}")
        # Show first 200 chars of content
        preview = r.content[:200].replace("\n", " ")
        if len(r.content) > 200:
            preview += "..."
        print(f"      {preview}")
        print()


async def _cmd_status() -> None:
    from src.models.db import init_pool, fetch_one, fetch_all, health_check, close_pool

    await init_pool()

    health = await health_check()
    print(f"Database: {health['status']}")

    if health["status"] == "healthy":
        chunk_count = await fetch_one("SELECT count(*) AS n FROM chunks")
        spaces = await fetch_all(
            """SELECT ms.name, count(c.id) AS chunks
               FROM memory_spaces ms
               LEFT JOIN chunks c ON c.space_id = ms.id
               GROUP BY ms.name ORDER BY ms.name"""
        )

        print(f"Total chunks: {chunk_count['n']}")
        print("Spaces:")
        for s in spaces:
            print(f"  {s['name']}: {s['chunks']} chunks")

    await close_pool()


if __name__ == "__main__":
    main()
