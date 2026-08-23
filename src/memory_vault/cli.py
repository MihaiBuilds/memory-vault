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
import sys

from memory_vault.logging_config import configure_logging

configure_logging()


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

    # purge-forgotten
    p_purge = sub.add_parser(
        "purge-forgotten",
        help="Permanently delete memories forgotten more than N days ago",
    )
    p_purge.add_argument(
        "--older-than",
        type=int,
        default=30,
        metavar="DAYS",
        help="Only purge memories forgotten at least this many days ago (default: 30)",
    )

    # mcp
    sub.add_parser("mcp", help="Start the MCP server (stdio transport)")

    # api
    sub.add_parser("api", help="Start the REST API server (uvicorn)")

    # token
    p_token = sub.add_parser("token", help="Manage API tokens")
    token_sub = p_token.add_subparsers(dest="token_cmd")

    p_tok_create = token_sub.add_parser("create", help="Create a new API token")
    p_tok_create.add_argument("name", help="A friendly name for the token")
    p_tok_create.add_argument(
        "--expires-in-days",
        type=int,
        default=None,
        metavar="N",
        help="Expire the token after N days (default: never expires)",
    )

    p_tok_revoke = token_sub.add_parser("revoke", help="Revoke a token by prefix")
    p_tok_revoke.add_argument("prefix", help="Token prefix (first 11 chars)")

    token_sub.add_parser("list", help="List existing tokens")

    # space
    p_space = sub.add_parser("space", help="Manage memory spaces")
    space_sub = p_space.add_subparsers(dest="space_cmd")

    p_space_create = space_sub.add_parser("create", help="Create a new memory space")
    p_space_create.add_argument("name", help="Space name (lowercase, hyphens allowed)")
    p_space_create.add_argument("--description", default=None, help="Optional description")

    space_sub.add_parser("list", help="List existing spaces")

    # diagnose
    p_diag = sub.add_parser(
        "diagnose",
        help="Bundle logs + status + config into a redacted zip for bug reports",
    )
    p_diag.add_argument(
        "--out-dir",
        default=None,
        help="Directory to write the zip into (default: current directory)",
    )

    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        sys.exit(1)

    if args.command == "migrate":
        asyncio.run(_cmd_migrate())
    elif args.command == "purge-forgotten":
        asyncio.run(_cmd_purge_forgotten(args.older_than))
    elif args.command == "ingest":
        asyncio.run(_cmd_ingest(args.file, args.space))
    elif args.command == "search":
        asyncio.run(_cmd_search(args.query, args.space, args.limit))
    elif args.command == "status":
        asyncio.run(_cmd_status())
    elif args.command == "mcp":
        from memory_vault.mcp.server import main as mcp_main

        mcp_main()
    elif args.command == "api":
        from memory_vault.api.server import main as api_main

        api_main()
    elif args.command == "token":
        if not args.token_cmd:
            p_token.print_help()
            sys.exit(1)
        asyncio.run(_cmd_token(args))
    elif args.command == "space":
        if not args.space_cmd:
            p_space.print_help()
            sys.exit(1)
        asyncio.run(_cmd_space(args))
    elif args.command == "diagnose":
        from pathlib import Path

        from memory_vault.diagnose import cli_diagnose

        cli_diagnose(Path(args.out_dir) if args.out_dir else None)


async def _cmd_purge_forgotten(older_than_days: int) -> None:
    """Permanently delete memories forgotten more than `older_than_days` ago.

    Exposed on the CLI as well as over MCP so an operator can schedule it —
    their cron, their retention policy, their machine. Memory Vault runs no
    timer of its own; deleting someone's notes should be something they asked
    for, not something that happens while they are not looking.
    """
    import json

    from memory_vault.mcp.server import purge_forgotten
    from memory_vault.models.db import close_pool, init_pool

    if older_than_days < 0:
        print("--older-than cannot be negative.")
        sys.exit(1)

    await init_pool()
    try:
        result = json.loads(await purge_forgotten(older_than_days=older_than_days))
        if not result.get("success"):
            print(f"Purge failed: {result.get('error', 'unknown error')}")
            sys.exit(1)
        print(
            f"Purged {result['purged']} memory(ies) forgotten more than "
            f"{older_than_days} day(s) ago. {result['remaining']} forgotten "
            f"memory(ies) remain."
        )
    finally:
        await close_pool()


async def _cmd_migrate() -> None:
    from memory_vault.config import settings
    from memory_vault.models.db import close_pool, init_pool, run_migrations

    # Migrations are the only path that needs DDL rights. When
    # DB_MIGRATION_USER is unset this is the same credential as the runtime
    # one, which is what every single-role deployment already does.
    if settings.db_migration_user:
        print(f"Applying migrations as: {settings.db_migration_user}")
    await init_pool(conninfo=settings.migration_database_url)
    await run_migrations()
    await close_pool()
    print("Migrations complete.")


async def _cmd_ingest(file_path: str, space: str) -> None:
    from pathlib import Path

    from memory_vault.models.db import close_pool, fetch_one, init_pool
    from memory_vault.services.ingestion import IngestionPipeline

    path = Path(file_path)
    if not path.exists():
        print(f"File not found: {file_path}")
        sys.exit(1)

    await init_pool()

    row = await fetch_one("SELECT id FROM memory_spaces WHERE name = %s", (space,))
    if not row:
        print(f"Unknown space: {space}")
        await close_pool()
        sys.exit(1)

    space_id = row["id"]
    pipeline = IngestionPipeline(max_workers=1)
    pipeline.enqueue(str(path.resolve()), space_id)
    stats = await pipeline.run_all()

    await close_pool()
    print(f"Ingested: {stats.chunks_created} chunks created, {stats.failed} failed")
    if stats.failed > 0:
        for err in stats.errors:
            print(f"  {err}")
        sys.exit(1)


async def _cmd_search(query: str, space: str | None, limit: int) -> None:
    from memory_vault.models.db import close_pool, init_pool
    from memory_vault.services.search import hybrid_search, resolve_space_names

    await init_pool()

    space_ids = await resolve_space_names([space] if space else None)
    results, variations, elapsed_ms = await hybrid_search(
        query,
        space_ids=space_ids if space else None,
        limit=limit,
    )

    await close_pool()

    print(f'\nSearch: "{query}"')
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
    from memory_vault.api.deps import create_token, revoke_token
    from memory_vault.models.db import close_pool, fetch_all, init_pool

    await init_pool()
    try:
        if args.token_cmd == "create":
            expires_in_days = getattr(args, "expires_in_days", None)
            if expires_in_days is not None and expires_in_days < 1:
                print("--expires-in-days must be 1 or greater.")
                sys.exit(1)
            plaintext = await create_token(args.name, expires_in_days)
            print("")
            print("  Token created. Copy it now — it will NOT be shown again.")
            print("")
            print(f"  Name:  {args.name}")
            print(f"  Token: {plaintext}")
            if expires_in_days is not None:
                print(f"  Expires: in {expires_in_days} day{'s' if expires_in_days != 1 else ''}")
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
                """SELECT name, token_prefix, created_at, last_used_at, revoked_at,
                          expires_at,
                          (expires_at IS NOT NULL AND expires_at <= now()) AS is_expired
                   FROM api_tokens ORDER BY created_at DESC"""
            )
            if not rows:
                print("No tokens yet. Create one with: memory-vault token create <name>")
                return
            print(f"{'NAME':<20} {'PREFIX':<14} {'CREATED':<22} {'EXPIRES':<22} {'STATUS'}")
            for r in rows:
                # Revoked wins over expired: revocation is a decision someone
                # made, expiry is just time passing.
                if r["revoked_at"]:
                    status_txt = "revoked"
                elif r["is_expired"]:
                    status_txt = "expired"
                else:
                    status_txt = "active"
                created = str(r["created_at"])[:19]
                expires = str(r["expires_at"])[:19] if r["expires_at"] else "never"
                print(
                    f"{r['name']:<20} {r['token_prefix']:<14} "
                    f"{created:<22} {expires:<22} {status_txt}"
                )
    finally:
        await close_pool()


async def _cmd_space(args) -> None:
    import re

    from memory_vault.models.db import close_pool, execute_query, fetch_all, fetch_one, init_pool

    await init_pool()
    try:
        if args.space_cmd == "create":
            name = args.name.strip()
            if not re.match(r"^[a-z0-9][a-z0-9-]*$", name) or len(name) > 64:
                print(
                    f"Invalid space name: {name!r}. Use lowercase letters, digits, hyphens (max 64)."
                )
                sys.exit(1)
            existing = await fetch_one("SELECT 1 FROM memory_spaces WHERE name = %s", (name,))
            if existing:
                print(f"Space already exists: {name}")
                sys.exit(1)
            await execute_query(
                "INSERT INTO memory_spaces (name, description) VALUES (%s, %s)",
                (name, args.description),
            )
            print(f"Space created: {name}")
        elif args.space_cmd == "list":
            rows = await fetch_all(
                """SELECT ms.name, ms.description, count(c.id) AS chunks
                   FROM memory_spaces ms
                   LEFT JOIN chunks c ON c.space_id = ms.id
                   GROUP BY ms.id, ms.name, ms.description
                   ORDER BY ms.name"""
            )
            if not rows:
                print("No spaces yet.")
                return
            print(f"{'NAME':<20} {'CHUNKS':<8} DESCRIPTION")
            for r in rows:
                print(f"{r['name']:<20} {r['chunks']:<8} {r['description'] or ''}")
    finally:
        await close_pool()


async def _cmd_status() -> None:
    from memory_vault.models.db import close_pool, fetch_all, fetch_one, health_check, init_pool

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
