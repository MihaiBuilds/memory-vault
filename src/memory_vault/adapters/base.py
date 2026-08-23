"""
Base classes for source adapters.

Every adapter converts a raw input (file contents, JSON, etc.)
into a list of RawChunks ready for embedding and storage.
"""

from __future__ import annotations

import hashlib
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime


@dataclass
class RawChunk:
    """A single chunk of text extracted from a source."""

    text: str
    speaker: str  # 'human', 'assistant', 'unknown'
    timestamp: datetime | None
    chunk_index: int
    metadata: dict = field(default_factory=dict)
    content_hash: str = ""

    def __post_init__(self) -> None:
        if not self.content_hash:
            self.content_hash = hashlib.sha256(self.text.encode()).hexdigest()


class SourceAdapter(ABC):
    """Abstract base for all source adapters."""

    @abstractmethod
    def parse(self, raw_input: str | bytes, source_path: str = "") -> list[RawChunk]:
        """Parse raw input into a list of RawChunks."""
        ...

    @abstractmethod
    def source_name(self) -> str:
        """Short identifier for this source type (e.g. 'markdown', 'plaintext')."""
        ...


def _word_count(text: str) -> int:
    return len(text.split())


def _split_long_text(
    text: str,
    max_words: int = 500,
    min_words: int = 100,
) -> list[str]:
    """Split text exceeding max_words into smaller pieces by paragraphs."""
    if _word_count(text) <= max_words:
        return [text]

    paragraphs = [p.strip() for p in text.split("\n\n") if p.strip()]
    chunks: list[str] = []
    current: list[str] = []
    current_wc = 0

    for para in paragraphs:
        para_wc = _word_count(para)

        if para_wc > max_words:
            if current:
                chunks.append("\n\n".join(current))
                current, current_wc = [], 0
            chunks.extend(_split_by_sentences(para, max_words))
            continue

        if current_wc + para_wc > max_words and current:
            chunks.append("\n\n".join(current))
            current, current_wc = [], 0

        current.append(para)
        current_wc += para_wc

    if current:
        chunks.append("\n\n".join(current))

    return chunks


def _split_by_words(text: str, max_words: int) -> list[str]:
    """Split on word boundaries, ignoring punctuation entirely.

    The floor under every other strategy. Paragraph and sentence splitting both
    need a boundary to cut on, and text can simply not have one — a wall of
    prose with no blank line and no terminator, minified content, a language
    this splitter has no rules for. Words always exist, so this always
    terminates and always respects the cap.
    """
    words = text.split()
    return [" ".join(words[i : i + max_words]) for i in range(0, len(words), max_words)]


def _split_by_sentences(text: str, max_words: int) -> list[str]:
    """Split by sentence boundaries, falling back to words when one is oversized."""
    import re

    sentences = re.split(r"(?<=[.!?])\s+", text)
    chunks: list[str] = []
    current: list[str] = []
    current_wc = 0

    for sent in sentences:
        sent_wc = _word_count(sent)

        # A single sentence over the cap has no sentence boundary to cut on, so
        # sentence splitting cannot help it. Previously it was appended whole
        # and `max_words` stopped being an upper bound — and because the flush
        # below is guarded on `current` being non-empty, an oversized FIRST
        # sentence escaped even when more text followed it.
        if sent_wc > max_words:
            if current:
                chunks.append(" ".join(current))
                current, current_wc = [], 0
            chunks.extend(_split_by_words(sent, max_words))
            continue

        if current_wc + sent_wc > max_words and current:
            chunks.append(" ".join(current))
            current, current_wc = [], 0
        current.append(sent)
        current_wc += sent_wc

    if current:
        chunks.append(" ".join(current))

    return chunks


def detect_adapter(file_path: str, content: str = "") -> SourceAdapter:
    """Auto-detect the right adapter for a file."""
    from memory_vault.adapters.claude import ClaudeJsonAdapter
    from memory_vault.adapters.markdown import MarkdownAdapter
    from memory_vault.adapters.plaintext import PlainTextAdapter

    path_lower = file_path.lower()

    if path_lower.endswith(".json"):
        # Check if it looks like Claude export JSON
        stripped = content.strip()
        if stripped.startswith("[") or stripped.startswith("{"):
            try:
                import json

                data = json.loads(stripped)
                # Claude export has chat_messages
                test = data if isinstance(data, list) else [data]
                if test and "chat_messages" in test[0]:
                    return ClaudeJsonAdapter()
            except (json.JSONDecodeError, KeyError, IndexError):
                # Not Claude JSON — fall through to PlainTextAdapter.
                pass
        return PlainTextAdapter()

    if path_lower.endswith(".md"):
        return MarkdownAdapter()

    return PlainTextAdapter()
