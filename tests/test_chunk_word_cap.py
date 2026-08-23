"""
`max_words` as a hard upper bound.

Paragraph splitting delegates an oversized paragraph to sentence splitting, and
sentence splitting had no fallback of its own: a sentence longer than the cap
was appended whole. The flush was also guarded on the accumulator being
non-empty, so an oversized *first* sentence escaped even when more text
followed it.

Word splitting is the floor. Paragraphs and sentences both need a boundary to
cut on and text can simply not have one — a wall of prose with no blank line
and no terminator, minified content, a language these rules do not cover.
Words always exist.
"""

from __future__ import annotations

import pytest

from memory_vault.adapters.base import _split_by_words, _split_long_text, _word_count

MAX = 500


def _sizes(chunks: list[str]) -> list[int]:
    return [_word_count(c) for c in chunks]


class TestCapIsNeverExceeded:
    def test_text_with_no_sentence_boundary(self):
        """The reported case: 600 words, no terminator anywhere."""
        chunks = _split_long_text("word " * 600, max_words=MAX)
        assert len(chunks) > 1, "600 words must not come back as one chunk"
        assert all(s <= MAX for s in _sizes(chunks)), _sizes(chunks)

    def test_oversized_first_sentence_followed_by_more(self):
        """
        Specifically the `and current` guard. With an empty accumulator the
        flush was skipped, so the first sentence escaped regardless of what
        came after it.
        """
        text = ("word " * 600).strip() + ". " + ("more " * 300)
        chunks = _split_long_text(text, max_words=MAX)
        assert all(s <= MAX for s in _sizes(chunks)), _sizes(chunks)

    def test_oversized_paragraph_without_sentences(self):
        chunks = _split_long_text("x " * 1200, max_words=MAX)
        assert all(s <= MAX for s in _sizes(chunks)), _sizes(chunks)

    @pytest.mark.parametrize("count", [501, 999, 1000, 2500])
    def test_various_lengths_stay_within_the_cap(self, count):
        chunks = _split_long_text("word " * count, max_words=MAX)
        assert all(s <= MAX for s in _sizes(chunks)), _sizes(chunks)

    def test_no_content_is_lost(self):
        """Splitting must repartition the words, not drop any."""
        text = "word " * 1234
        chunks = _split_long_text(text, max_words=MAX)
        assert sum(_sizes(chunks)) == 1234


class TestOrdinaryTextIsUnchanged:
    """
    Every adapter calls this on every ingest, so the risk is not the bug — it
    is changing how text that already split correctly gets chunked.
    """

    def test_short_text_returns_one_chunk(self):
        text = "This is a short paragraph. It has two sentences."
        assert _split_long_text(text, max_words=MAX) == [text]

    def test_exactly_at_the_cap_is_one_chunk(self):
        text = ("word " * MAX).strip()
        assert len(_split_long_text(text, max_words=MAX)) == 1

    def test_normal_prose_splits_on_sentences_not_words(self):
        """
        Sentence boundaries must still be preferred. If word splitting had
        taken over, chunks would end mid-sentence.
        """
        chunks = _split_long_text("This is a sentence. " * 200, max_words=MAX)
        assert all(s <= MAX for s in _sizes(chunks))
        assert all(c.rstrip().endswith(".") for c in chunks), (
            "sentence splitting should still cut on sentence ends"
        )

    def test_paragraph_structure_is_still_preferred(self):
        """Paragraphs under the cap are merged, not word-split."""
        text = "\n\n".join(f"Paragraph {i}. " + "word " * 80 for i in range(10))
        chunks = _split_long_text(text, max_words=MAX)
        assert all(s <= MAX for s in _sizes(chunks))
        assert any("\n\n" in c for c in chunks), "paragraph joins should survive"


class TestSplitByWords:
    def test_splits_into_full_sized_pieces(self):
        chunks = _split_by_words("word " * 1000, 400)
        assert _sizes(chunks) == [400, 400, 200]

    def test_short_input_is_a_single_chunk(self):
        assert _split_by_words("a b c", 400) == ["a b c"]

    def test_empty_input_yields_nothing(self):
        assert _split_by_words("", 400) == []

    def test_collapses_irregular_whitespace(self):
        """`str.split()` handles runs of spaces, tabs and newlines alike."""
        assert _split_by_words("a\t\tb\n\nc   d", 2) == ["a b", "c d"]
