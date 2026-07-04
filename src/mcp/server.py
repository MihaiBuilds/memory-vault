"""
MCP server for Memory Vault.

Exposes memory search, storage, and management to Claude Desktop and Claude Code
via the Model Context Protocol (stdio transport).

Tools:
    recall           — search memory with hybrid search (vector + full-text + RRF)
    remember         — store a new memory (auto-creates unknown spaces)
    forget           — soft-delete a memory chunk
    move_memory      — move a chunk to a different space without re-embedding
    supersede_memory — replace a chunk; old is hidden from recall by default
    migrate_tags     — migrate [space:X]-prefixed chunks to named spaces
    memory_status    — system health + statistics

Resources:
    memory://spaces — list of memory spaces
    memory://stats  — current memory statistics
"""

from __future__ import annotations

import decimal
import hashlib
import json
import logging
import re
import sys
import uuid
from datetime import UTC, datetime
from pathlib import Path

from dotenv import load_dotenv

# Load .env from project root
load_dotenv(Path(__file__).parents[2] / ".env")

# Ensure project root is on sys.path for imports
_project_root = str(Path(__file__).parents[2])
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)

from mcp.server.fastmcp import FastMCP  # noqa: E402

from src.models.db import (  # noqa: E402
    execute_query,
    fetch_all,
    fetch_one,
    has_column,
    health_check,
    init_pool,
)
from src.services.embedding import MODEL_NAME, embed  # noqa: E402
from src.services.search import hybrid_search, resolve_space_names  # noqa: E402

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("mcp.memory-vault")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _json_default(obj):
    """Handle Decimal and datetime in JSON serialization."""
    if isinstance(obj, decimal.Decimal):
        return float(obj)
    if isinstance(obj, datetime):
        return obj.isoformat()
    raise TypeError(f"Object of type {type(obj).__name__} is not JSON serializable")


def _dumps(obj, **kw):
    return json.dumps(obj, default=_json_default, **kw)


def _estimate_tokens(text: str) -> int:
    return len(text) // 4


def _budget_results(results: list[dict], max_tokens: int) -> tuple[list[dict], bool]:
    """
    Fit results within a token budget.
    Top results get full content (up to 60% budget), rest get truncated.
    """
    if not results:
        return results, False

    budgeted = []
    tokens_used = 0
    full_budget = int(max_tokens * 0.6)
    truncated = False

    for r in results:
        content = r["content"]
        entry_tokens = _estimate_tokens(content) + 40

        if tokens_used < full_budget:
            budgeted.append(r)
            tokens_used += entry_tokens
        elif tokens_used < max_tokens:
            truncated = True
            r_copy = dict(r)
            if len(content) > 200:
                r_copy["content"] = content[:200] + "... [truncated]"
            budgeted.append(r_copy)
            tokens_used += _estimate_tokens(r_copy["content"]) + 40
        else:
            truncated = True
            break

    return budgeted, truncated


# ---------------------------------------------------------------------------
# MCP server instance
# ---------------------------------------------------------------------------

mcp = FastMCP("memory-vault")

# ---------------------------------------------------------------------------
# DB lifecycle
# ---------------------------------------------------------------------------

_db_ready = False


async def _ensure_db() -> bool:
    """Initialize the database pool if not already done."""
    global _db_ready
    if not _db_ready:
        try:
            await init_pool(min_size=1, max_size=5)
            _db_ready = True
        except Exception as e:
            logger.error("Failed to connect to database: %s", e)
            return False
    return True


# ---------------------------------------------------------------------------
# Tool: recall
# ---------------------------------------------------------------------------


@mcp.tool()
async def recall(
    query: str,
    spaces: list[str] | None = None,
    since: str | None = None,
    limit: int = 10,
    max_tokens: int = 2000,
    include_superseded: bool = False,
) -> str:
    """
    Search your memories for information relevant to a query.

    Returns chunks ranked by relevance using hybrid search (vector + full-text + RRF).
    Uses query enrichment (keyword extraction + variation) for better recall.
    Results are budgeted to fit within max_tokens to avoid flooding context.

    Args:
        query: The search query — a question, topic, or keyword phrase.
        spaces: Filter to specific memory spaces (e.g. ["default", "projects"]).
                If omitted, searches all spaces.
        since: Only return memories after this date (ISO format, e.g. "2025-01-01").
        limit: Maximum number of results (default 10, max 50).
        max_tokens: Token budget for results (default 2000).
        include_superseded: If True, include chunks that have been superseded
                            (hidden by default). Use for audit/history.
    """
    if not await _ensure_db():
        return _dumps(
            {
                "status": "offline",
                "results": [],
                "message": "Database is not available.",
            }
        )

    try:
        space_ids = await resolve_space_names(spaces) if spaces else None

        since_dt = None
        if since:
            try:
                since_dt = datetime.fromisoformat(since).replace(tzinfo=UTC)
            except ValueError:
                return _dumps(
                    {"error": f"Invalid date format: {since}. Use ISO format (YYYY-MM-DD)."}
                )

        limit = min(max(limit, 1), 50)
        max_tokens = min(max(max_tokens, 200), 8000)

        results, variations, elapsed_ms = await hybrid_search(
            query_text=query,
            space_ids=space_ids,
            since=since_dt,
            limit=limit,
            include_superseded=include_superseded,
        )

        formatted = []
        for r in results:
            entry = {
                "chunk_id": r.chunk_id,
                "content": r.content,
                "similarity": r.similarity,
                "space": r.space,
                "speaker": r.speaker,
                "source": r.source,
                "created_at": str(r.created_at) if r.created_at else None,
            }
            if r.metadata.get("heading"):
                entry["section_heading"] = r.metadata["heading"]
            formatted.append(entry)

        budgeted, was_truncated = _budget_results(formatted, max_tokens)

        response = {
            "status": "ok",
            "results": budgeted,
            "total_results": len(results),
            "results_shown": len(budgeted),
            "query_time_ms": elapsed_ms,
            "query_variations": variations,
        }
        if was_truncated:
            response["note"] = (
                f"Some results were truncated to fit within {max_tokens} token budget. "
                "Use max_tokens to increase."
            )

        return _dumps(response, indent=2)

    except Exception as e:
        logger.exception("recall failed")
        return _dumps({"status": "error", "results": [], "message": f"Search failed: {e}"})


# ---------------------------------------------------------------------------
# Tool: remember
# ---------------------------------------------------------------------------


@mcp.tool()
async def remember(
    text: str,
    space: str = "default",
    source: str = "mcp",
    speaker: str = "human",
) -> str:
    """
    Store a new memory in the system.

    The text is embedded and stored as a searchable chunk.
    Use this to save important information, decisions, or knowledge.

    Args:
        text: The text content to remember.
        space: Which memory space to store it in (default "default").
        source: Where this memory comes from (default "mcp").
        speaker: Who said/wrote this — "human" or "assistant" (default "human").
    """
    if not await _ensure_db():
        return _dumps({"stored": False, "error": "Database offline"})

    try:
        space_id = await _ensure_space(space)

        # Embed
        embedding = embed(text)
        content_hash = hashlib.sha256(text.encode()).hexdigest()

        # Check for exact duplicate (same hash in same space)
        dup = await fetch_one(
            """SELECT id FROM chunks
               WHERE space_id = %s
                 AND metadata->>'content_hash' = %s""",
            (space_id, content_hash),
        )
        if dup:
            return _dumps(
                {
                    "stored": False,
                    "duplicate": True,
                    "existing_chunk_id": str(dup["id"]),
                    "message": "This memory already exists (exact duplicate).",
                }
            )

        # Classify
        category, importance = _classify_memory(text)
        meta = json.dumps({"category": category, "source": source, "content_hash": content_hash})

        chunk_id = str(uuid.uuid4())

        await execute_query(
            """INSERT INTO chunks
                   (id, space_id, chunk_index, speaker, content, embedding,
                    source, importance, metadata)
               VALUES (%s, %s, 0, %s, %s, %s::vector, %s, %s, %s::jsonb)""",
            (chunk_id, space_id, speaker, text, str(embedding), f"mcp:{source}", importance, meta),
        )

        return _dumps(
            {
                "stored": True,
                "chunk_id": chunk_id,
                "space": space,
                "category": category,
                "importance": importance,
                "message": "Memory stored successfully.",
            }
        )

    except Exception as e:
        logger.exception("remember failed")
        return _dumps({"stored": False, "error": str(e)})


def _classify_memory(text: str) -> tuple[str, float]:
    """Classify a memory into a category and assign importance."""
    t = text.lower()

    if any(
        w in t
        for w in (
            "decided",
            "decision",
            "agreed",
            "chose",
            "will use",
            "going with",
            "picked",
            "committed to",
            "locked",
        )
    ):
        return "decision", 0.8

    if any(
        w in t
        for w in (
            "learned",
            "lesson",
            "mistake",
            "insight",
            "realized",
            "discovered",
            "takeaway",
            "never again",
        )
    ):
        return "lesson", 0.75

    if any(
        w in t
        for w in (
            "prefer",
            "always use",
            "convention",
            "never use",
            "style",
            "rule",
            "must",
            "non-negotiable",
        )
    ):
        return "preference", 0.7

    if any(
        w in t
        for w in (
            "pattern",
            "approach",
            "technique",
            "architecture",
            "strategy",
            "workflow",
            "pipeline",
            "design",
        )
    ):
        return "pattern", 0.7

    return "fact", 0.5


# ---------------------------------------------------------------------------
# Space auto-create helper (shared by remember / move_memory / supersede_memory)
# ---------------------------------------------------------------------------

_SPACE_TAG_RE = re.compile(r"^\[space:([a-zA-Z0-9_-]+)\]\s*")

# The real multi-space model (auto-created spaces, move_memory, supersede_memory,
# migrate_tags) ships together with migrations/004_supersede_move.sql — bundled as
# one upgrade in the feature branch, and gated together here. Checking for
# `is_superseded` is a proxy for "has migration 004 been applied", used uniformly
# below rather than probing each new column individually.
_UPGRADE_UNAVAILABLE_SUFFIX = (
    "unavailable until the pending schema migration "
    "(migrations/004_supersede_move.sql) is applied to this database. "
    "Use the [space:X] text-tag convention within the 'default' space for now "
    "(see migrate_tags)."
)


async def _upgrade_schema_ready() -> bool:
    """Whether migrations/004_supersede_move.sql has been applied to this database."""
    return await has_column("chunks", "is_superseded")


async def _ensure_space(name: str) -> int:
    """
    Return the space ID for `name`.

    Creating a NEW space is deferred behind the pending schema migration (see
    `_UPGRADE_UNAVAILABLE_SUFFIX`) — until then, only ALREADY-EXISTING spaces (in
    practice, just "default") can be resolved. Raises ValueError for an unknown
    name so callers (remember / move_memory / supersede_memory) surface a clear
    message instead of silently fragmenting the interim [space:X]-tag convention,
    which stays the operative model for now.
    """
    row = await fetch_one("SELECT id FROM memory_spaces WHERE name = %s", (name,))
    if row:
        return row["id"]

    if not await _upgrade_schema_ready():
        raise ValueError(
            f"memory space {name!r} does not exist yet, and creating new spaces is "
            f"{_UPGRADE_UNAVAILABLE_SUFFIX}"
        )

    await execute_query(
        "INSERT INTO memory_spaces (name) VALUES (%s) ON CONFLICT (name) DO NOTHING",
        (name,),
    )
    row = await fetch_one("SELECT id FROM memory_spaces WHERE name = %s", (name,))
    return row["id"]


# ---------------------------------------------------------------------------
# Tool: move_memory
# ---------------------------------------------------------------------------


@mcp.tool()
async def move_memory(chunk_id: str, to_space: str) -> str:
    """
    Move a chunk to a different memory space without re-embedding.

    Preserves: chunk_id, content, embedding, created_at, importance, category.
    The chunk no longer appears in searches scoped to its original space.

    Args:
        chunk_id: UUID of the chunk to move.
        to_space: Name of the destination space (auto-created if new).
    """
    if not await _ensure_db():
        return _dumps({"success": False, "error": "Database offline"})

    if not await _upgrade_schema_ready():
        return _dumps({"success": False, "error": f"move_memory is {_UPGRADE_UNAVAILABLE_SUFFIX}"})

    try:
        row = await fetch_one("SELECT id FROM chunks WHERE id = %s", (chunk_id,))
        if not row:
            return _dumps({"success": False, "error": f"Chunk {chunk_id} not found."})

        new_space_id = await _ensure_space(to_space)
        await execute_query(
            "UPDATE chunks SET space_id = %s, updated_at = now() WHERE id = %s",
            (new_space_id, chunk_id),
        )
        return _dumps({"success": True, "chunk_id": chunk_id, "moved_to": to_space})

    except Exception as e:
        logger.exception("move_memory failed")
        return _dumps({"success": False, "error": str(e)})


# ---------------------------------------------------------------------------
# Tool: supersede_memory
# ---------------------------------------------------------------------------


@mcp.tool()
async def supersede_memory(old_chunk_id: str, new_text: str, space: str | None = None) -> str:
    """
    Replace a chunk with updated content.

    The new chunk is embedded and stored. The old chunk is marked is_superseded=true
    and excluded from recall by default. Use recall(include_superseded=True) to
    retrieve superseded chunks for audit.

    Args:
        old_chunk_id: UUID of the chunk to supersede.
        new_text: Replacement memory text (re-embedded).
        space: Space for the new chunk. Defaults to the old chunk's space.
    """
    if not await _ensure_db():
        return _dumps({"stored": False, "error": "Database offline"})

    if not await _upgrade_schema_ready():
        return _dumps(
            {"stored": False, "error": f"supersede_memory is {_UPGRADE_UNAVAILABLE_SUFFIX}"}
        )

    try:
        old_row = await fetch_one(
            "SELECT id, space_id, speaker, importance FROM chunks WHERE id = %s",
            (old_chunk_id,),
        )
        if not old_row:
            return _dumps({"stored": False, "error": f"Chunk {old_chunk_id} not found."})

        new_space_id = await _ensure_space(space) if space else old_row["space_id"]

        embedding = embed(new_text)
        content_hash = hashlib.sha256(new_text.encode()).hexdigest()
        category, importance = _classify_memory(new_text)
        meta = json.dumps({
            "category": category,
            "source": "mcp",
            "content_hash": content_hash,
            "supersedes": old_chunk_id,
        })
        new_chunk_id = str(uuid.uuid4())
        await execute_query(
            """INSERT INTO chunks
                   (id, space_id, chunk_index, speaker, content, embedding,
                    source, importance, metadata)
               VALUES (%s, %s, 0, %s, %s, %s::vector, %s, %s, %s::jsonb)""",
            (
                new_chunk_id, new_space_id,
                old_row["speaker"] or "human",
                new_text, str(embedding),
                "mcp:supersede",
                importance, meta,
            ),
        )
        await execute_query(
            "UPDATE chunks SET is_superseded = true, superseded_by = %s, updated_at = now() "
            "WHERE id = %s",
            (new_chunk_id, old_chunk_id),
        )
        return _dumps({
            "stored": True,
            "new_chunk_id": new_chunk_id,
            "superseded_chunk_id": old_chunk_id,
            "message": f"Superseded {old_chunk_id!r} → {new_chunk_id!r}",
        })

    except Exception as e:
        logger.exception("supersede_memory failed")
        return _dumps({"stored": False, "error": str(e)})


# ---------------------------------------------------------------------------
# Tool: migrate_tags
# ---------------------------------------------------------------------------


@mcp.tool()
async def migrate_tags() -> str:
    """
    Migrate [space:X]-prefixed chunks from `default` to their named spaces.

    For each chunk in `default` whose content starts with `[space:X]`:
      1. Ensure space X exists (auto-created if new).
      2. Move the chunk to space X (update space_id).
      3. Strip the `[space:X]` prefix and re-embed the stripped content.

    Untagged chunks and chunks already outside `default` are skipped.
    Idempotent: a second run finds no tagged chunks and returns moved=0.

    Returns: {"moved": N, "skipped_untagged": M}
    """
    if not await _ensure_db():
        return _dumps({"error": "Database offline"})

    if not await _upgrade_schema_ready():
        return _dumps({"error": f"migrate_tags is {_UPGRADE_UNAVAILABLE_SUFFIX}"})

    try:
        rows = await fetch_all(
            """SELECT c.id, c.content
               FROM chunks c
               JOIN memory_spaces ms ON ms.id = c.space_id
               WHERE ms.name = 'default'
                 AND c.importance > 0
                 AND (c.metadata->>'forgotten')::boolean IS NOT TRUE
                 AND c.is_superseded = false
               ORDER BY c.created_at"""
        )

        moved = 0
        skipped_untagged = 0

        for row in rows:
            m = _SPACE_TAG_RE.match(row["content"])
            if not m:
                skipped_untagged += 1
                continue

            target_space = m.group(1)
            stripped = row["content"][m.end():]
            new_space_id = await _ensure_space(target_space)
            new_embedding = embed(stripped)
            content_hash = hashlib.sha256(stripped.encode()).hexdigest()

            await execute_query(
                """UPDATE chunks
                   SET space_id = %s,
                       content = %s,
                       embedding = %s::vector,
                       metadata = metadata || %s::jsonb,
                       updated_at = now()
                   WHERE id = %s""",
                (
                    new_space_id,
                    stripped,
                    str(new_embedding),
                    json.dumps({"content_hash": content_hash, "migrated_from_tag": True}),
                    row["id"],
                ),
            )
            moved += 1

        return _dumps({
            "moved": moved,
            "skipped_untagged": skipped_untagged,
            "message": (
                f"Migrated {moved} chunk(s) to named spaces; "
                f"{skipped_untagged} untagged chunk(s) left in default."
            ),
        })

    except Exception as e:
        logger.exception("migrate_tags failed")
        return _dumps({"error": str(e)})


# ---------------------------------------------------------------------------
# Tool: forget
# ---------------------------------------------------------------------------


@mcp.tool()
async def forget(chunk_id: str) -> str:
    """
    Soft-delete a memory chunk by ID.

    The chunk is removed from search results but stays in the database
    for potential recovery. Sets importance to 0 and marks it in metadata.

    Args:
        chunk_id: The UUID of the chunk to forget.
    """
    if not await _ensure_db():
        return _dumps({"success": False, "error": "Database offline"})

    try:
        row = await fetch_one(
            "SELECT id, content, metadata FROM chunks WHERE id = %s",
            (chunk_id,),
        )
        if not row:
            return _dumps({"success": False, "error": f"Chunk {chunk_id} not found."})

        meta = row["metadata"] or {}
        if isinstance(meta, str):
            meta = json.loads(meta)

        if meta.get("forgotten"):
            return _dumps({"success": False, "error": f"Chunk {chunk_id} is already forgotten."})

        meta["forgotten"] = True
        meta["forgotten_at"] = datetime.now(UTC).isoformat()

        await execute_query(
            """UPDATE chunks
               SET importance = 0,
                   metadata = %s::jsonb,
                   updated_at = now()
               WHERE id = %s""",
            (json.dumps(meta), chunk_id),
        )

        preview = row["content"][:80] + "..." if len(row["content"]) > 80 else row["content"]
        return _dumps(
            {
                "success": True,
                "chunk_id": chunk_id,
                "message": f'Memory forgotten: "{preview}"',
            }
        )

    except Exception as e:
        logger.exception("forget failed")
        return _dumps({"success": False, "error": str(e)})


# ---------------------------------------------------------------------------
# Tool: memory_status
# ---------------------------------------------------------------------------


@mcp.tool()
async def memory_status() -> str:
    """
    Get the current status of the memory system.

    Returns database health, chunk counts per space, and embedding model info.
    """
    if not await _ensure_db():
        return _dumps({"status": "offline", "message": "Cannot connect to database."})

    try:
        db_status = await health_check()
        db_ok = db_status["status"] == "healthy"

        rows = await fetch_all("""
            SELECT ms.name,
                   COUNT(c.id) AS total,
                   COUNT(c.id) FILTER (
                       WHERE c.importance > 0
                         AND (c.metadata->>'forgotten')::boolean IS NOT TRUE
                   ) AS active
            FROM memory_spaces ms
            LEFT JOIN chunks c ON c.space_id = ms.id
            GROUP BY ms.name
            ORDER BY ms.name
        """)

        spaces = {}
        total_chunks = 0
        active_chunks = 0
        for r in rows:
            spaces[r["name"]] = {"total": r["total"], "active": r["active"]}
            total_chunks += r["total"]
            active_chunks += r["active"]

        # Recent query stats
        ql = await fetch_one("""
            SELECT COUNT(*) AS cnt, AVG(latency_ms) AS avg_lat
            FROM query_log
            WHERE created_at >= now() - interval '24 hours'
        """)

        return _dumps(
            {
                "status": "online" if db_ok else "degraded",
                "database": "connected" if db_ok else "error",
                "embedding_model": MODEL_NAME,
                "total_chunks": total_chunks,
                "active_chunks": active_chunks,
                "chunks_per_space": spaces,
                "queries_24h": ql["cnt"] if ql else 0,
                "avg_latency_ms": round(float(ql["avg_lat"]), 1) if ql and ql["avg_lat"] else None,
            },
            indent=2,
        )

    except Exception as e:
        logger.exception("memory_status failed")
        return _dumps({"status": "error", "message": str(e)})


# ---------------------------------------------------------------------------
# Resource: memory://spaces
# ---------------------------------------------------------------------------


@mcp.resource("memory://spaces")
async def list_spaces() -> str:
    """List all memory spaces with descriptions and chunk counts."""
    if not await _ensure_db():
        return _dumps([])

    rows = await fetch_all("""
        SELECT ms.name, ms.description,
               COUNT(c.id) FILTER (
                   WHERE c.importance > 0
                     AND (c.metadata->>'forgotten')::boolean IS NOT TRUE
               ) AS chunk_count
        FROM memory_spaces ms
        LEFT JOIN chunks c ON c.space_id = ms.id
        GROUP BY ms.id, ms.name, ms.description
        ORDER BY ms.name
    """)

    return _dumps(
        [
            {
                "name": r["name"],
                "description": r["description"],
                "chunk_count": r["chunk_count"],
            }
            for r in rows
        ],
        indent=2,
    )


# ---------------------------------------------------------------------------
# Resource: memory://stats
# ---------------------------------------------------------------------------


@mcp.resource("memory://stats")
async def memory_stats() -> str:
    """Current memory system statistics — chunks, queries, latency."""
    if not await _ensure_db():
        return _dumps({"status": "offline"})

    rows = await fetch_all("""
        SELECT ms.name,
               COUNT(c.id) FILTER (
                   WHERE c.importance > 0
                     AND (c.metadata->>'forgotten')::boolean IS NOT TRUE
               ) AS active
        FROM memory_spaces ms
        LEFT JOIN chunks c ON c.space_id = ms.id
        GROUP BY ms.name ORDER BY ms.name
    """)

    ql = await fetch_one("""
        SELECT COUNT(*) AS cnt, AVG(latency_ms) AS avg_lat,
               COUNT(*) FILTER (WHERE result_count = 0) AS zero_results
        FROM query_log
        WHERE created_at >= now() - interval '24 hours'
    """)

    return _dumps(
        {
            "chunks_per_space": {r["name"]: r["active"] for r in rows},
            "total_active_chunks": sum(r["active"] for r in rows),
            "queries_24h": ql["cnt"] if ql else 0,
            "avg_latency_ms": round(float(ql["avg_lat"]), 1) if ql and ql["avg_lat"] else None,
            "zero_result_queries_24h": ql["zero_results"] if ql else 0,
        },
        indent=2,
    )


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main():
    mcp.run()


if __name__ == "__main__":
    main()
