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
    speaker: str              # 'human', 'assistant', 'unknown'
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


def _split_by_sentences(text: str, max_words: int) -> list[str]:
    """Last-resort split by sentence boundaries."""
    import re

    sentences = re.split(r"(?<=[.!?])\s+", text)
    chunks: list[str] = []
    current: list[str] = []
    current_wc = 0

    for sent in sentences:
        sent_wc = _word_count(sent)
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
    from src.adapters.markdown import MarkdownAdapter
    from src.adapters.plaintext import PlainTextAdapter
    from src.adapters.claude import ClaudeJsonAdapter

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
                pass
        return PlainTextAdapter()

    if path_lower.endswith(".md"):
        return MarkdownAdapter()

    return PlainTextAdapter()
