"""
Tests for the search engine — query expansion and tsquery building.

These tests don't require a database connection — they test the pure logic.
Integration tests with the full hybrid search pipeline require a running PostgreSQL.
"""

import pytest

from src.services.search import expand_query, _build_tsquery, _STOP_WORDS


class TestExpandQuery:
    def test_returns_at_least_original(self):
        variations = expand_query("test query")
        assert len(variations) >= 1
        assert variations[0] == "test query"

    def test_question_expanded(self):
        variations = expand_query("How does PostgreSQL handle vector indexing with HNSW?")
        assert len(variations) >= 2
        assert variations[0] == "How does PostgreSQL handle vector indexing with HNSW?"

    def test_technical_terms_extracted(self):
        variations = expand_query("How does pgvector handle HNSW indexing for semantic search?")
        assert len(variations) >= 2
        all_text = " ".join(variations[1:])
        assert any(term in all_text for term in ["pgvector", "HNSW", "indexing", "semantic"])

    def test_short_query(self):
        variations = expand_query("pgvector")
        assert len(variations) >= 1

    def test_max_three_variations(self):
        variations = expand_query("What are the best practices for building scalable vector search systems?")
        assert len(variations) <= 3


class TestBuildTsquery:
    def test_basic_terms(self):
        result = _build_tsquery("PostgreSQL vector search")
        assert result is not None
        assert "PostgreSQL" in result
        assert "vector" in result
        assert "search" in result
        assert "&" in result

    def test_stop_words_filtered(self):
        result = _build_tsquery("what is the best database")
        assert result is not None
        # "what", "is", "the" are stop words
        assert "best" in result
        assert "database" in result

    def test_numbers_kept(self):
        result = _build_tsquery("vector 384 dimensions embedding")
        assert result is not None
        assert "384" in result

    def test_empty_after_filtering(self):
        result = _build_tsquery("what is the")
        assert result is None

    def test_pure_stop_words(self):
        result = _build_tsquery("and or but the")
        assert result is None
