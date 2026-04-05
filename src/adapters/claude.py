"""
Adapter for Claude conversation export JSON.

Handles both single conversation objects and arrays of conversations.
Expected format: Claude Desktop/API export with chat_messages array.
"""

from __future__ import annotations

import json
from datetime import datetime

from .base import RawChunk, SourceAdapter, _split_long_text

_SPEAKER_MAP = {
    "human": "human",
    "assistant": "assistant",
}


class ClaudeJsonAdapter(SourceAdapter):

    def source_name(self) -> str:
        return "claude"

    def parse(self, raw_input: str | bytes, source_path: str = "") -> list[RawChunk]:
        data = json.loads(raw_input)

        if isinstance(data, dict):
            conversations = [data]
        elif isinstance(data, list):
            conversations = data
        else:
            raise ValueError(f"Unexpected JSON root type: {type(data)}")

        chunks: list[RawChunk] = []
        global_index = 0

        for conv in conversations:
            conv_id = conv.get("uuid", "")
            conv_title = conv.get("name", "")
            conv_created = _parse_ts(conv.get("created_at"))

            for msg_idx, msg in enumerate(conv.get("chat_messages", [])):
                text = (msg.get("text") or "").strip()
                if not text:
                    continue

                sender = msg.get("sender", "unknown")
                speaker = _SPEAKER_MAP.get(sender, sender)
                msg_ts = _parse_ts(msg.get("created_at"))

                base_meta = {
                    "source_file": source_path,
                    "source_msg_index": msg_idx,
                    "conversation_id": conv_id,
                    "conversation_title": conv_title,
                    "source": "claude",
                }

                parts = _split_long_text(text, max_words=500)
                for part_text in parts:
                    chunks.append(RawChunk(
                        text=part_text,
                        speaker=speaker,
                        timestamp=msg_ts or conv_created,
                        chunk_index=global_index,
                        metadata={**base_meta},
                    ))
                    global_index += 1

        return chunks


def _parse_ts(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None
