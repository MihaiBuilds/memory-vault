"""
Markdown adapter — splits by headings, then by horizontal rules.

Sections over 500 words are further split by paragraphs.
"""

from __future__ import annotations

import re
from datetime import datetime

from .base import RawChunk, SourceAdapter, _split_long_text

_HEADING_RE = re.compile(r"^(#{1,6})\s+(.*)", re.MULTILINE)
_HR_RE = re.compile(r"^\s*(?:---+|\*\*\*+|___+)\s*$", re.MULTILINE)


class MarkdownAdapter(SourceAdapter):

    def __init__(self, default_speaker: str = "human") -> None:
        self._speaker = default_speaker

    def source_name(self) -> str:
        return "markdown"

    def parse(self, raw_input: str | bytes, source_path: str = "") -> list[RawChunk]:
        if isinstance(raw_input, bytes):
            raw_input = raw_input.decode("utf-8", errors="replace")

        sections = self._split_by_headings(raw_input)
        chunks: list[RawChunk] = []
        chunk_index = 0

        for heading, level, body in sections:
            body = body.strip()
            if not body:
                continue

            base_meta = {
                "source_file": source_path,
                "source": "markdown",
                "heading": heading,
                "heading_level": level,
            }

            ts = _extract_date(heading)

            for sub in _split_on_hr(body):
                sub = sub.strip()
                if not sub:
                    continue

                parts = _split_long_text(sub, max_words=500)
                for part_text in parts:
                    chunks.append(RawChunk(
                        text=part_text,
                        speaker=self._speaker,
                        timestamp=ts,
                        chunk_index=chunk_index,
                        metadata={**base_meta, "source_msg_index": chunk_index},
                    ))
                    chunk_index += 1

        return chunks

    def _split_by_headings(self, text: str) -> list[tuple[str, int, str]]:
        matches = list(_HEADING_RE.finditer(text))

        if not matches:
            return [("", 0, text)]

        sections: list[tuple[str, int, str]] = []

        pre = text[: matches[0].start()].strip()
        if pre:
            sections.append(("", 0, pre))

        for i, m in enumerate(matches):
            level = len(m.group(1))
            heading = m.group(2).strip()
            start = m.end()
            end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
            body = text[start:end].strip()
            sections.append((heading, level, body))

        return sections


def _split_on_hr(text: str) -> list[str]:
    parts = _HR_RE.split(text)
    return [p for p in parts if p.strip()]


def _extract_date(heading: str) -> datetime | None:
    m = re.search(r"(\d{4}-\d{2}-\d{2})", heading)
    if m:
        try:
            return datetime.fromisoformat(m.group(1))
        except ValueError:
            pass
    return None
