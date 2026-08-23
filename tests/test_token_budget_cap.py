"""
Token budgets as hard caps.

Both budget helpers could admit one arbitrarily oversized result:

  - MCP `_budget_results` appends a result in full whenever
    `tokens_used < full_budget`, without comparing that result's size to the
    remaining budget. The first result always satisfies that condition.
  - Chat `_apply_token_budget` deliberately keeps at least one result, but
    never trimmed the one it kept.

Keeping one result is right — an answer with no context is worse than one with
trimmed context. Sending it whole is not.
"""

from __future__ import annotations

import pytest

from memory_vault.api.routers.chat import (
    _PROMPT_TOKEN_BUDGET,
    SYSTEM_PROMPT,
    _apply_token_budget,
    _format_context_block,
)
from memory_vault.api.routers.chat import _estimate_tokens as chat_tokens
from memory_vault.mcp.server import _budget_results, _estimate_tokens, _truncate_to_tokens
from memory_vault.services.search import SearchResult


def _result(content: str, similarity: float = 0.9) -> SearchResult:
    return SearchResult(
        chunk_id="c1",
        content=content,
        similarity=similarity,
        speaker=None,
        space="s",
        source=None,
        created_at=None,
        metadata={},
    )


def _chat_prompt_tokens(results: list[SearchResult], question: str = "q") -> int:
    """Mirror of the estimate inside _apply_token_budget, minus history."""
    return (
        chat_tokens(SYSTEM_PROMPT)
        + chat_tokens(_format_context_block(results))
        + chat_tokens(question)
        + 200
    )


class TestMcpBudget:
    @pytest.mark.parametrize(
        "size,budget",
        [(10_000, 200), (100_000, 500), (10_000, 1_000), (5_000, 200)],
    )
    def test_single_oversized_result_is_capped(self, size, budget):
        """The reported case: one huge result against a small budget."""
        budgeted, truncated = _budget_results([{"content": "x" * size}], max_tokens=budget)
        used = _estimate_tokens(budgeted[0]["content"]) + 40
        assert used <= budget, f"{used} tokens exceeds the {budget} budget"
        assert truncated is True, "the caller must be told content was cut"

    def test_small_result_is_untouched(self):
        """The fix must not trim results that already fit."""
        content = "y" * 300
        budgeted, truncated = _budget_results([{"content": content}], max_tokens=1_000)
        assert budgeted[0]["content"] == content
        assert truncated is False

    def test_many_results_stay_within_budget(self):
        results = [{"content": "z" * 4_000} for _ in range(10)]
        budgeted, truncated = _budget_results(results, max_tokens=1_000)
        total = sum(_estimate_tokens(r["content"]) + 40 for r in budgeted)
        assert total <= 1_000, f"{total} tokens exceeds the 1000 budget"
        assert truncated is True

    def test_empty_results_are_passed_through(self):
        assert _budget_results([], max_tokens=200) == ([], False)


class TestTruncateToTokens:
    def test_result_fits_the_allowance(self):
        out = _truncate_to_tokens("x" * 10_000, 100)
        assert _estimate_tokens(out) <= 100

    def test_marker_is_inside_the_allowance_not_added_to_it(self):
        """
        The marker is part of what gets sent. Counting it separately would mean
        truncating to the limit and still exceeding it.
        """
        out = _truncate_to_tokens("x" * 10_000, 50)
        assert out.endswith("... [truncated]")
        assert _estimate_tokens(out) <= 50

    def test_short_text_is_returned_unchanged(self):
        assert _truncate_to_tokens("short", 100) == "short"

    @pytest.mark.parametrize("allowance", [0, -5])
    def test_no_allowance_yields_only_the_marker(self, allowance):
        assert _truncate_to_tokens("x" * 100, allowance) == "... [truncated]"


class TestChatBudget:
    @pytest.mark.parametrize("size", [100_000, 50_000, 30_000])
    def test_single_oversized_result_is_trimmed(self, size):
        _, results = _apply_token_budget("q", [], [_result("x" * size)])
        assert len(results) == 1, "one result should still be kept"
        assert _chat_prompt_tokens(results) <= _PROMPT_TOKEN_BUDGET

    def test_at_least_one_result_survives(self):
        """
        Trimming must not become dropping. An answer with no context at all is
        worse than one with trimmed context.
        """
        _, results = _apply_token_budget("q", [], [_result("x" * 500_000)])
        assert len(results) == 1
        assert results[0].content, "the kept result must not be emptied"

    def test_result_that_fits_is_untouched(self):
        content = "a short memory"
        _, results = _apply_token_budget("q", [], [_result(content)])
        assert results[0].content == content

    def test_history_is_still_dropped_before_content_is_cut(self):
        """
        Trimming the last result is the final step, not the first. History
        should go first — it is cheaper to lose than retrieved context.
        """
        from memory_vault.api.schemas import ChatMessage

        # Enough history to blow the budget on its own, with a result that
        # comfortably fits. Dropping the history alone should resolve it.
        history = [ChatMessage(role="user", content="old " * 8_000)]
        content = "y" * 4_000
        kept_history, results = _apply_token_budget("q", history, [_result(content)])

        assert kept_history == [], "history should be dropped first"
        assert results[0].content == content, "content should not be cut unnecessarily"

    def test_multiple_results_are_dropped_before_trimming(self):
        """Lowest-similarity results go before the top one gets cut."""
        results_in = [
            _result("a" * 8_000, similarity=0.9),
            _result("b" * 8_000, similarity=0.5),
            _result("c" * 8_000, similarity=0.1),
        ]
        _, results = _apply_token_budget("q", [], results_in)
        assert _chat_prompt_tokens(results) <= _PROMPT_TOKEN_BUDGET
        assert results[0].content.startswith("a"), "the top result should be the survivor"
