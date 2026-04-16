"""
CLI entrypoint — memory-vault command with subcommands.

Usage:
    memory-vault ingest <file> [--space default]
    memory-vault search <query> [--space default] [--limit 5]
    memory-vault status
    memory-vault migrate
    memory-vault api
    memory-vault token create <name>
    memory-vault token revoke <prefix>
    memory-vault token list
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

    # mcp
    sub.add_parser("mcp", help="Start the MCP server (stdio transport)")

    # api
    sub.add_parser("api", help="Start the REST API server (uvicorn)")

    # token
    p_token = sub.add_parser("token", help="Manage API tokens")
    token_sub = p_token.add_subparsers(dest="token_cmd")

    p_tok_create = token_sub.add_parser("create", help="Create a new API token")
    p_tok_create.add_argument("name", help="A friendly name for the token")

    p_tok_revoke = token_sub.add_parser("revoke", help="Revoke a token by prefix")
    p_tok_revoke.add_argument("prefix", help="Token prefix (first 11 chars)")

    token_sub.add_parser("list", help="List existing tokens")

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
    elif args.command == "mcp":
        from src.mcp.server import main as mcp_main
        mcp_main()
    elif args.command == "api":
        from src.api.server import main as api_main
        api_main()
    elif args.command == "token":
        if not args.token_cmd:
            p_token.print_help()
            sys.exit(1)
        asyncio.run(_cmd_token(args))


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


async def _cmd_token(args) -> None:
    from src.models.db import init_pool, close_pool, fetch_all
    from src.api.deps import create_token, revoke_token

    await init_pool()
    try:
        if args.token_cmd == "create":
            plaintext = await create_token(args.name)
            print("")
            print("  Token created. Copy it now — it will NOT be shown again.")
            print("")
            print(f"  Name:  {args.name}")
            print(f"  Token: {plaintext}")
            print("")
            print("  Use it with: Authorization: Bearer <token>")
            print("")
        elif args.token_cmd == "revoke":
            ok = await revoke_token(args.prefix)
            if ok:
                print(f"Token revoked: {args.prefix}")
            else:
                print(f"No active token with prefix: {args.prefix}")
                sys.exit(1)
        elif args.token_cmd == "list":
            rows = await fetch_all(
                """SELECT name, token_prefix, created_at, last_used_at, revoked_at
                   FROM api_tokens ORDER BY created_at DESC"""
            )
            if not rows:
                print("No tokens yet. Create one with: memory-vault token create <name>")
                return
            print(f"{'NAME':<20} {'PREFIX':<14} {'CREATED':<22} {'STATUS'}")
            for r in rows:
                status_txt = "revoked" if r["revoked_at"] else "active"
                created = str(r["created_at"])[:19]
                print(f"{r['name']:<20} {r['token_prefix']:<14} {created:<22} {status_txt}")
    finally:
        await close_pool()


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
