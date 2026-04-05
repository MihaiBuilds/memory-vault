"""
Hybrid search engine — vector similarity + full-text keyword matching + RRF merging.

expand_query():   generates query variations using tokenizer-based keyword extraction.
hybrid_search():  combines vector (HNSW) + full-text (tsvector GIN), merges via RRF.
log_query():      writes to query_log for observability.
"""

from __future__ import annotations

import json
import re
import time
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from src.config import settings
from src.models.db import fetch_one, fetch_all, execute_query
from src.services.embedding import embed, embed_batch, _get_model

logger = logging.getLogger(__name__)

_STOP_WORDS = {
    "what", "how", "why", "when", "where", "who", "which", "whom", "whose",
    "does", "do", "did", "is", "are", "was", "were", "be", "been", "being",
    "have", "has", "had", "can", "could", "should", "would", "will", "shall",
    "may", "might", "must", "the", "a", "an", "and", "or", "but", "not",
    "in", "on", "at", "to", "for", "of", "with", "by", "from", "as",
    "if", "then", "than", "that", "this", "it", "its", "my", "your",
    "his", "her", "our", "their", "i", "you", "he", "she", "we", "they",
    "me", "him", "us", "them", "about", "into", "through", "during",
    "before", "after", "up", "down", "out", "off", "over", "under",
    "again", "there", "here", "some", "any", "all", "each", "every",
    "no", "nor", "so", "very", "just", "also", "like", "use", "using",
    "used", "get", "set", "make", "take", "give", "tell", "say",
}

_RRF_K = settings.rrf_k
_FTS_WEIGHT = 0.5
_IMPORTANCE_WEIGHT = 0.15
_RECENCY_HALF_LIFE_DAYS = 90
_RECENCY_MAX_BOOST = 0.05


@dataclass
class SearchResult:
    chunk_id: str
    content: str
    similarity: float
    speaker: str | None
    space: str
    source: str | None
    created_at: datetime | None
    metadata: dict = field(default_factory=dict)


def expand_query(query_text: str) -> list[str]:
    """
    Generate up to 3 query variations:
      [0] Original query
      [1] Keyword-focused (extracted key terms)
      [2] Broader contextual (question -> statement)
    """
    variations = [query_text]

    keywords = _extract_key_terms(query_text)

    if keywords:
        variations.append(" ".join(keywords))

    broad = _make_broad_variation(query_text, keywords)
    if broad and broad != query_text and broad != variations[-1]:
        variations.append(broad)

    if len(variations) < 2:
        cleaned = re.sub(r"[?!.,;:]", "", query_text).strip()
        if cleaned != query_text:
            variations.append(cleaned)

    return variations[:3]


def _extract_key_terms(text: str) -> list[str]:
    """Use the tokenizer to find content-bearing words via WordPiece subword analysis."""
    model = _get_model()
    tokenizer = model.tokenizer

    tokens = tokenizer.tokenize(text)

    words: list[tuple[str, int]] = []
    current_word_parts: list[str] = []

    for tok in tokens:
        if tok.startswith("##"):
            current_word_parts.append(tok[2:])
        else:
            if current_word_parts:
                word = "".join(current_word_parts)
                words.append((word, len(current_word_parts)))
            current_word_parts = [tok]

    if current_word_parts:
        word = "".join(current_word_parts)
        words.append((word, len(current_word_parts)))

    key_terms = []
    seen: set[str] = set()
    for word, subword_count in sorted(words, key=lambda w: w[1], reverse=True):
        lower = word.lower()
        if (
            lower not in _STOP_WORDS
            and lower not in seen
            and len(word) >= 3
            and not word.isdigit()
        ):
            original = _find_original_case(text, word)
            key_terms.append(original)
            seen.add(lower)

    return key_terms


def _find_original_case(text: str, word: str) -> str:
    pattern = re.compile(re.escape(word), re.IGNORECASE)
    match = pattern.search(text)
    return match.group(0) if match else word


def _make_broad_variation(query: str, keywords: list[str]) -> str:
    q = query.strip().rstrip("?")
    q_lower = q.lower()

    for prefix in ("what ", "how ", "why ", "when ", "where ", "who ",
                   "which ", "does ", "do ", "is ", "are ", "can ",
                   "could ", "should ", "tell me about "):
        if q_lower.startswith(prefix):
            q = q[len(prefix):]
            break

    if len(keywords) >= 2:
        return f"{q} — {' '.join(keywords[:4])}"

    return q


def _build_where_clause(
    space_ids: list[int] | None,
    since: datetime | None,
) -> tuple[list[str], list[Any]]:
    """Build shared WHERE clauses for both search arms."""
    where_clauses: list[str] = []
    params: list[Any] = []

    if space_ids:
        placeholders = ", ".join(["%s"] * len(space_ids))
        where_clauses.append(f"c.space_id IN ({placeholders})")
        params.extend(space_ids)

    if since:
        where_clauses.append("c.created_at >= %s")
        params.append(since)

    return where_clauses, params


async def hybrid_search(
    query_text: str,
    space_ids: list[int] | None = None,
    since: datetime | None = None,
    limit: int | None = None,
    *,
    enrich: bool = True,
) -> tuple[list[SearchResult], list[str], int]:
    """
    Hybrid search: vector (HNSW) + full-text (tsvector GIN) + RRF merging.

    Returns: (results, query_variations, elapsed_ms)
    """
    if limit is None:
        limit = settings.search_default_limit

    start = time.perf_counter()

    # Generate variations and embed
    variations = expand_query(query_text) if enrich else [query_text]
    vectors = embed_batch(variations) if len(variations) > 1 else [embed(variations[0])]

    base_where, base_params = _build_where_clause(space_ids, since)

    # --- Arm 1: Vector search (multi-variation UNION ALL) ---

    vec_limit = limit * 3
    union_parts: list[str] = []
    vec_params: list[Any] = []

    for i, vec in enumerate(vectors):
        vec_str = str(vec)
        part_params: list[Any] = [vec_str]
        part_params.extend(base_params)
        part_params.extend([vec_str, vec_limit])

        where_sql = " AND ".join(base_where) if base_where else "TRUE"

        union_parts.append(f"""(
            SELECT c.id AS chunk_id, c.content, c.speaker,
                   c.source, c.created_at,
                   ms.name AS space,
                   c.metadata, c.importance,
                   1 - (c.embedding <=> %s::vector) AS similarity,
                   {i} AS variation_idx
            FROM chunks c
            JOIN memory_spaces ms ON ms.id = c.space_id
            WHERE {where_sql}
            ORDER BY c.embedding <=> %s::vector
            LIMIT %s
        )""")
        vec_params.extend(part_params)

    vec_sql = f"""
        SELECT DISTINCT ON (chunk_id)
               chunk_id, content, speaker, source,
               created_at, space, similarity, metadata, importance
        FROM (
            {" UNION ALL ".join(union_parts)}
        ) AS merged
        ORDER BY chunk_id, similarity DESC
    """
    vec_sql = f"""
        SELECT *, ROW_NUMBER() OVER (ORDER BY similarity DESC) AS rank
        FROM ({vec_sql}) AS deduped
    """

    vec_rows = await fetch_all(vec_sql, tuple(vec_params))

    # --- Arm 2: Full-text search ---

    fts_rows: list[dict] = []
    tsquery = _build_tsquery(query_text)
    if tsquery:
        fts_where = list(base_where)
        fts_where.append("c.content_tsv @@ to_tsquery('english', %s)")
        fts_where_sql = " AND ".join(fts_where)

        fts_params: list[Any] = [tsquery, tsquery]
        fts_params.extend(base_params)
        fts_params.append(tsquery)
        fts_params.append(vec_limit)

        fts_sql = f"""
            SELECT c.id AS chunk_id, c.content, c.speaker,
                   c.source, c.created_at,
                   ms.name AS space,
                   c.metadata, c.importance,
                   ts_rank(c.content_tsv, to_tsquery('english', %s)) AS fts_rank,
                   ROW_NUMBER() OVER (
                       ORDER BY ts_rank(c.content_tsv, to_tsquery('english', %s)) DESC
                   ) AS rank
            FROM chunks c
            JOIN memory_spaces ms ON ms.id = c.space_id
            WHERE {fts_where_sql}
            LIMIT %s
        """
        fts_rows = await fetch_all(fts_sql, tuple(fts_params))

    # --- Merge via Reciprocal Rank Fusion ---

    candidates: dict[str, dict] = {}

    for row in vec_rows:
        cid = str(row["chunk_id"])
        candidates[cid] = {"row": row, "vec_rank": row["rank"], "fts_rank": None}

    for row in fts_rows:
        cid = str(row["chunk_id"])
        if cid in candidates:
            candidates[cid]["fts_rank"] = row["rank"]
        else:
            candidates[cid] = {"row": row, "vec_rank": None, "fts_rank": row["rank"]}

    now_utc = datetime.now(timezone.utc)
    scored: list[tuple[float, str, dict]] = []

    for cid, info in candidates.items():
        rrf = 0.0
        if info["vec_rank"] is not None:
            rrf += 1.0 / (_RRF_K + info["vec_rank"])
        if info["fts_rank"] is not None:
            rrf += _FTS_WEIGHT / (_RRF_K + info["fts_rank"])

        row = info["row"]
        importance = row.get("importance") or 0.5
        rrf += _IMPORTANCE_WEIGHT * float(importance)

        created = row.get("created_at")
        if created:
            if created.tzinfo is None:
                created = created.replace(tzinfo=timezone.utc)
            age_days = max((now_utc - created).days, 0)
            recency_boost = 1.0 / (1.0 + age_days / _RECENCY_HALF_LIFE_DAYS)
            rrf += _RECENCY_MAX_BOOST * recency_boost

        scored.append((rrf, cid, info))

    scored.sort(key=lambda x: x[0], reverse=True)

    # --- Build results ---

    elapsed_ms = int((time.perf_counter() - start) * 1000)

    results = []
    for rrf_score, cid, info in scored[:limit]:
        row = info["row"]

        meta = row.get("metadata") or {}
        if isinstance(meta, str):
            try:
                meta = json.loads(meta)
            except (json.JSONDecodeError, TypeError):
                meta = {}

        if info["vec_rank"] is not None:
            sim = round(row.get("similarity", 0.0), 4)
        else:
            sim = round(0.3 + (0.1 / (info["fts_rank"] or 1)), 4)

        results.append(SearchResult(
            chunk_id=cid,
            content=row["content"],
            similarity=sim,
            speaker=row["speaker"],
            space=row["space"],
            source=row["source"],
            created_at=row["created_at"],
            metadata=meta,
        ))

    return results, variations, elapsed_ms


def _build_tsquery(query_text: str) -> str | None:
    """Build a PostgreSQL tsquery string. Uses & (AND) between terms."""
    words = re.findall(r"[a-zA-Z0-9]+", query_text)
    terms = [w for w in words if w.lower() not in _STOP_WORDS or w.isdigit()]
    if not terms:
        return None
    return " & ".join(terms)


async def log_query(
    query_text: str,
    space_ids: list[int] | None,
    results: list[SearchResult],
    latency_ms: int,
) -> None:
    """Write to query_log for observability."""
    try:
        await execute_query(
            """INSERT INTO query_log
                   (query_text, space_ids, result_count, top_similarity, latency_ms)
               VALUES (%s, %s, %s, %s, %s)""",
            (
                query_text,
                space_ids or None,
                len(results),
                results[0].similarity if results else None,
                latency_ms,
            ),
        )
    except Exception:
        logger.exception("Failed to log query")


async def resolve_space_names(names: list[str] | None) -> list[int]:
    """Convert space names to IDs."""
    if not names:
        return []
    ids = []
    for name in names:
        row = await fetch_one(
            "SELECT id FROM memory_spaces WHERE name = %s", (name,)
        )
        if row:
            ids.append(row["id"])
    return ids
