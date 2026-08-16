"""Thinking-block filtering survives tags split across stream chunks."""

import json

import pytest

from memory_vault.api.routers import chat

# Applied per-test rather than module-wide: the helper test below is synchronous
# and a module-level mark would flag it as a mismarked async test.
asyncio_test = pytest.mark.asyncio


class _FakeStream:
    """Minimal stand-in for httpx's streaming response context manager."""

    def __init__(self, pieces: list[str]):
        self._pieces = pieces

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc_info):
        return False

    def raise_for_status(self) -> None:
        return None

    async def aiter_lines(self):
        for piece in self._pieces:
            payload = {"choices": [{"delta": {"content": piece}}]}
            yield f"data: {json.dumps(payload)}"
        yield "data: [DONE]"


class _FakeClient:
    def __init__(self, pieces: list[str]):
        self._pieces = pieces

    def stream(self, *args, **kwargs):
        return _FakeStream(self._pieces)


async def _collect(pieces: list[str]) -> str:
    client = _FakeClient(pieces)
    out = [
        chunk
        async for chunk in chat._stream_openai_compat(client, "http://x", {}, {"messages": []})
    ]
    return "".join(out)


@asyncio_test
async def test_closing_tag_split_across_chunks_keeps_the_answer():
    """The reported case: "</thi" + "nk>ANSWER" must still yield the answer.

    Regression guard for #98. While inside a thinking block the parser cleared
    the whole buffer whenever no complete "</think>" was present, discarding a
    trailing "</thi" that the next chunk would have completed. The closing tag
    was then never recognised and the answer was filtered away as reasoning.
    """
    assert await _collect(["<think>secret</thi", "nk>ANSWER"]) == "ANSWER"


@pytest.mark.parametrize("split_at", range(1, len("</think>")))
@asyncio_test
async def test_closing_tag_split_at_every_position(split_at: int):
    """The answer survives a split at any point inside the closing tag."""
    tag = "</think>"
    pieces = ["<think>reasoning" + tag[:split_at], tag[split_at:] + "ANSWER"]
    assert await _collect(pieces) == "ANSWER"


@pytest.mark.parametrize("split_at", range(1, len("<think>")))
@asyncio_test
async def test_opening_tag_split_at_every_position(split_at: int):
    """A split opening tag still suppresses the reasoning that follows it."""
    tag = "<think>"
    pieces = [tag[:split_at], tag[split_at:] + "reasoning</think>ANSWER"]
    assert await _collect(pieces) == "ANSWER"


@asyncio_test
async def test_unsplit_stream_is_unchanged():
    """The ordinary single-chunk case keeps working."""
    assert await _collect(["<think>reasoning</think>ANSWER"]) == "ANSWER"


@asyncio_test
async def test_text_without_thinking_passes_through():
    assert await _collect(["plain ", "answer"]) == "plain answer"


@asyncio_test
async def test_angle_bracket_near_the_end_is_not_swallowed():
    """A trailing "<" that cannot begin a think tag still reaches the reader.

    The buffer used to hold back its last seven characters whenever they
    contained any "<". At the end of a stream nothing arrives to flush that
    tail, so a chunk ending in something like "a < b" lost those characters
    entirely rather than merely delaying them.
    """
    assert await _collect(["result: a < b"]) == "result: a < b"


@asyncio_test
async def test_angle_bracket_mid_stream_passes_through():
    assert await _collect(["if a < b then", " done"]) == "if a < b then done"


@asyncio_test
async def test_reasoning_never_leaks_when_stream_ends_mid_block():
    """A stream that ends inside a thinking block emits nothing from it."""
    assert await _collect(["<think>secret reasoning"]) == ""


@asyncio_test
async def test_partial_closing_tag_at_stream_end_is_not_emitted():
    """A dangling "</thi" is reasoning, not answer text, so it stays hidden."""
    assert await _collect(["<think>secret</thi"]) == ""


def test_partial_tag_suffix_matches_only_real_prefixes():
    """The helper keeps a tail only when it could actually begin the tag."""
    assert chat._partial_tag_suffix("reasoning</thi", "</think>") == "</thi"
    assert chat._partial_tag_suffix("reasoning<", "</think>") == "<"
    assert chat._partial_tag_suffix("reasoning", "</think>") == ""
    assert chat._partial_tag_suffix("a < b", "</think>") == ""
    # A complete tag is the caller's business to find; the helper holds only
    # strict prefixes, never the whole tag.
    assert chat._partial_tag_suffix("x</think>", "</think>") == ""
