"""
Plain text adapter — fallback for unrecognized file types.

Splits by paragraphs (double newlines) and horizontal rules (---).
Merges short paragraphs, splits long ones. Target: 100-500 words per chunk.
"""

from __future__ import annotations

import re

from .base import RawChunk, SourceAdapter, _split_long_text, _word_count

_HR_RE = re.compile(r"\n\s*---+\s*\n")


class PlainTextAdapter(SourceAdapter):

    def __init__(self, default_speaker: str = "human") -> None:
        self._speaker = default_speaker

    def source_name(self) -> str:
        return "plaintext"

    def parse(self, raw_input: str | bytes, source_path: str = "") -> list[RawChunk]:
        if isinstance(raw_input, bytes):
            raw_input = raw_input.decode("utf-8", errors="replace")

        sections = _HR_RE.split(raw_input)
        chunks: list[RawChunk] = []
        chunk_index = 0

        for section in sections:
            section = section.strip()
            if not section:
                continue

            paragraphs = [p.strip() for p in section.split("\n\n") if p.strip()]
            merged = self._merge_short(paragraphs, min_words=100, max_words=500)

            for block in merged:
                parts = _split_long_text(block, max_words=500)
                for part in parts:
                    chunks.append(RawChunk(
                        text=part,
                        speaker=self._speaker,
                        timestamp=None,
                        chunk_index=chunk_index,
                        metadata={
                            "source_file": source_path,
                            "source": "plaintext",
                            "source_msg_index": chunk_index,
                        },
                    ))
                    chunk_index += 1

        return chunks

    @staticmethod
    def _merge_short(
        paragraphs: list[str],
        min_words: int,
        max_words: int,
    ) -> list[str]:
        if not paragraphs:
            return []

        merged: list[str] = []
        current: list[str] = []
        current_wc = 0

        for para in paragraphs:
            wc = _word_count(para)

            if current_wc + wc > max_words and current:
                merged.append("\n\n".join(current))
                current, current_wc = [], 0

            current.append(para)
            current_wc += wc

            if current_wc >= min_words:
                merged.append("\n\n".join(current))
                current, current_wc = [], 0

        if current:
            merged.append("\n\n".join(current))

        return merged
