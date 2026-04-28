"""
Regression tests for migration 003 (knowledge graph schema).

These tests inspect the live test DB schema after migrations have run and
assert the exact shape M7 locks. If someone accidentally drops a column,
renames a constraint, or removes an index, these tests catch it.
"""

from __future__ import annotations

import pytest

from src.models.db import fetch_all, fetch_one


pytestmark = pytest.mark.asyncio


async def test_entities_table_has_space_id_not_null():
    col = await fetch_one(
        """SELECT is_nullable
           FROM information_schema.columns
           WHERE table_name = 'entities' AND column_name = 'space_id'"""
    )
    assert col is not None, "entities.space_id column missing"
    assert col["is_nullable"] == "NO", "entities.space_id must be NOT NULL"


async def test_entities_unique_index_is_per_space():
    # The unique index should be on (lower(name), type, space_id), not on
    # the pre-M7 (lower(name), type) shape.
    idx = await fetch_one(
        """SELECT indexdef
           FROM pg_indexes
           WHERE tablename = 'entities' AND indexname = 'entities_name_type_space_idx'"""
    )
    assert idx is not None, "entities_name_type_space_idx missing"
    assert "lower(name)" in idx["indexdef"]
    assert "type" in idx["indexdef"]
    assert "space_id" in idx["indexdef"]
    assert "UNIQUE" in idx["indexdef"].upper()

    # And the old global unique index must be gone.
    old = await fetch_one(
        """SELECT 1
           FROM pg_indexes
           WHERE tablename = 'entities' AND indexname = 'entities_name_type_idx'"""
    )
    assert old is None, "old entities_name_type_idx should have been dropped"


async def test_relationships_has_renamed_columns_and_chunk_id():
    cols = await fetch_all(
        """SELECT column_name, is_nullable
           FROM information_schema.columns
           WHERE table_name = 'relationships'
           ORDER BY column_name"""
    )
    names = {c["column_name"] for c in cols}

    # Post-M7 canonical column names.
    assert "source_entity_id" in names, "source_entity_id missing (pre-M7 from_entity_id?)"
    assert "target_entity_id" in names, "target_entity_id missing (pre-M7 to_entity_id?)"
    assert "type" in names, "type missing (pre-M7 rel_type?)"
    assert "chunk_id" in names, "chunk_id FK missing"

    # Pre-M7 column names must be gone.
    assert "from_entity_id" not in names
    assert "to_entity_id" not in names
    assert "rel_type" not in names

    # chunk_id is nullable per M7 lock (future manual tagging won't always have a chunk).
    by_name = {c["column_name"]: c["is_nullable"] for c in cols}
    assert by_name["chunk_id"] == "YES"


async def test_entity_mentions_table_exists_with_correct_schema():
    cols = await fetch_all(
        """SELECT column_name, is_nullable
           FROM information_schema.columns
           WHERE table_name = 'entity_mentions'
           ORDER BY column_name"""
    )
    assert cols, "entity_mentions table missing"
    by_name = {c["column_name"]: c["is_nullable"] for c in cols}
    # Required columns must all be NOT NULL.
    assert by_name.get("entity_id") == "NO"
    assert by_name.get("chunk_id") == "NO"
    assert by_name.get("start_offset") == "NO"
    assert by_name.get("end_offset") == "NO"
    assert "id" in by_name
    assert "created_at" in by_name
