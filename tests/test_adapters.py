"""
Tests for source adapters — Markdown, PlainText, Claude JSON, and auto-detection.
"""

import json
import pytest

from src.adapters.base import RawChunk, _word_count, detect_adapter
from src.adapters.markdown import MarkdownAdapter
from src.adapters.plaintext import PlainTextAdapter
from src.adapters.claude import ClaudeJsonAdapter


class TestRawChunk:
    def test_content_hash_auto_generated(self):
        chunk = RawChunk(text="hello world", speaker="human", timestamp=None, chunk_index=0)
        assert chunk.content_hash
        assert len(chunk.content_hash) == 64  # SHA-256

    def test_identical_text_same_hash(self):
        c1 = RawChunk(text="hello world", speaker="human", timestamp=None, chunk_index=0)
        c2 = RawChunk(text="hello world", speaker="assistant", timestamp=None, chunk_index=1)
        assert c1.content_hash == c2.content_hash

    def test_different_text_different_hash(self):
        c1 = RawChunk(text="hello", speaker="human", timestamp=None, chunk_index=0)
        c2 = RawChunk(text="world", speaker="human", timestamp=None, chunk_index=0)
        assert c1.content_hash != c2.content_hash


class TestMarkdownAdapter:
    def setup_method(self):
        self.adapter = MarkdownAdapter()

    def test_source_name(self):
        assert self.adapter.source_name() == "markdown"

    def test_basic_parse(self):
        md = """## Section One

Content of section one.

## Section Two

Content of section two.
"""
        chunks = self.adapter.parse(md, source_path="test.md")
        assert len(chunks) == 2
        assert "section one" in chunks[0].text.lower()
        assert "section two" in chunks[1].text.lower()

    def test_heading_metadata(self):
        md = """## 2025-03-01 — Morning

Today I worked on the project.
"""
        chunks = self.adapter.parse(md, source_path="journal.md")
        assert len(chunks) == 1
        assert chunks[0].metadata["heading"] == "2025-03-01 — Morning"
        assert chunks[0].timestamp is not None
        assert chunks[0].timestamp.year == 2025

    def test_preamble_captured(self):
        md = """Some preamble text before headings.

## First Section

Section content.
"""
        chunks = self.adapter.parse(md, source_path="test.md")
        assert len(chunks) == 2
        assert "preamble" in chunks[0].text.lower()

    def test_long_section_split(self):
        long_body = "\n\n".join([f"Paragraph {i}. " + "word " * 80 for i in range(10)])
        md = f"## Long Section\n\n{long_body}"
        chunks = self.adapter.parse(md, source_path="long.md")
        assert len(chunks) > 1
        assert all(_word_count(c.text) <= 550 for c in chunks)  # allow small margin

    def test_source_file_in_metadata(self):
        md = "## Test\n\nContent here."
        chunks = self.adapter.parse(md, source_path="notes/test.md")
        assert all(c.metadata["source_file"] == "notes/test.md" for c in chunks)


class TestPlainTextAdapter:
    def setup_method(self):
        self.adapter = PlainTextAdapter()

    def test_source_name(self):
        assert self.adapter.source_name() == "plaintext"

    def test_short_paragraphs_merged(self):
        text = "\n\n".join([
            "First paragraph about the project.",
            "Second paragraph about design.",
            "Third paragraph about implementation.",
        ])
        chunks = self.adapter.parse(text, source_path="notes.txt")
        assert len(chunks) == 1  # short paragraphs merge

    def test_hr_splits(self):
        text = "Section one content.\n\n---\n\nSection two content."
        chunks = self.adapter.parse(text, source_path="notes.txt")
        assert len(chunks) == 2

    def test_bytes_input(self):
        chunks = self.adapter.parse(b"Hello world content here.", source_path="test.txt")
        assert len(chunks) == 1
        assert "Hello world" in chunks[0].text


class TestClaudeJsonAdapter:
    def setup_method(self):
        self.adapter = ClaudeJsonAdapter()

    def test_source_name(self):
        assert self.adapter.source_name() == "claude"

    def test_single_conversation(self):
        data = json.dumps({
            "uuid": "conv-1",
            "name": "Test Conversation",
            "created_at": "2025-03-01T10:00:00Z",
            "chat_messages": [
                {"uuid": "m1", "text": "Hello Claude", "sender": "human",
                 "created_at": "2025-03-01T10:00:01Z"},
                {"uuid": "m2", "text": "Hello! How can I help?", "sender": "assistant",
                 "created_at": "2025-03-01T10:00:02Z"},
            ],
        })
        chunks = self.adapter.parse(data, source_path="conv.json")
        assert len(chunks) == 2
        assert chunks[0].speaker == "human"
        assert chunks[1].speaker == "assistant"

    def test_conversation_array(self):
        data = json.dumps([
            {
                "uuid": "c1", "name": "Conv 1", "created_at": "2025-01-01T00:00:00Z",
                "chat_messages": [
                    {"uuid": "m1", "text": "Hello", "sender": "human"},
                ],
            },
            {
                "uuid": "c2", "name": "Conv 2", "created_at": "2025-01-02T00:00:00Z",
                "chat_messages": [
                    {"uuid": "m2", "text": "World", "sender": "assistant"},
                ],
            },
        ])
        chunks = self.adapter.parse(data, source_path="export.json")
        assert len(chunks) == 2

    def test_empty_messages_skipped(self):
        data = json.dumps({
            "uuid": "c1", "name": "Test",
            "chat_messages": [
                {"uuid": "m1", "text": "", "sender": "human"},
                {"uuid": "m2", "text": "Real content", "sender": "assistant"},
            ],
        })
        chunks = self.adapter.parse(data, source_path="test.json")
        assert len(chunks) == 1

    def test_metadata_populated(self):
        data = json.dumps({
            "uuid": "conv-123", "name": "My Conv",
            "created_at": "2025-06-01T00:00:00Z",
            "chat_messages": [
                {"uuid": "msg-1", "text": "Test message", "sender": "human",
                 "created_at": "2025-06-01T00:01:00Z"},
            ],
        })
        chunks = self.adapter.parse(data, source_path="test.json")
        meta = chunks[0].metadata
        assert meta["conversation_id"] == "conv-123"
        assert meta["conversation_title"] == "My Conv"
        assert meta["source"] == "claude"


class TestAutoDetection:
    def test_markdown_detected(self):
        assert isinstance(detect_adapter("notes.md"), MarkdownAdapter)

    def test_plaintext_fallback(self):
        assert isinstance(detect_adapter("notes.txt"), PlainTextAdapter)
        assert isinstance(detect_adapter("unknown.xyz"), PlainTextAdapter)

    def test_claude_json_detected(self):
        content = json.dumps([{"uuid": "x", "chat_messages": [], "name": "test"}])
        assert isinstance(detect_adapter("data.json", content=content), ClaudeJsonAdapter)

    def test_generic_json_fallback(self):
        content = json.dumps({"key": "value"})
        assert isinstance(detect_adapter("data.json", content=content), PlainTextAdapter)
