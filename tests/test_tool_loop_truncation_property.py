"""Property 8 性质测试：工具结果截断（Req 8.5 / 8.6）。

**Validates: Requirements 8.5, 8.6**

用 `hypothesis` 生成随机超长工具结果与随机预算，断言 `truncate_to_tokens`
回灌的 tool 文本满足：

- 任意输入下 `counter.count(返回值) <= max_tokens + counter.count(note)`
  （即回灌 tool 消息 token ≤ `max_tool_result_tokens + len(note)` 的实际口径，
  实现以 `counter.count(note)` 计量备注开销）；
- 原文本已在预算内（`count(text) <= max_tokens`）时原样返回；
- 真正发生截断时，保留截断标记（备注 `note`）。
"""

from __future__ import annotations

from hypothesis import given, settings
from hypothesis import strategies as st

from paper_agent.agents.tool_loop import truncate_to_tokens
from paper_agent.context.tokenizer import HeuristicTokenCounter

# 固定的截断标记（含原始 token 数等信息的备注），与实现中的备注语义一致。
_NOTE = "\n\n[结果过长已截断：保留前约 N tokens，可用 read_section 取全文]"


@settings(max_examples=200)
@given(
    text=st.text(min_size=0, max_size=4000),
    max_tokens=st.integers(min_value=1, max_value=200),
)
def test_truncated_tool_result_respects_token_bound(text: str, max_tokens: int):
    """回灌 tool 文本 token ≤ max_tokens + count(note)（任意输入）。"""
    counter = HeuristicTokenCounter()
    result = truncate_to_tokens(text, max_tokens, counter, note=_NOTE)
    assert counter.count(result) <= max_tokens + counter.count(_NOTE)


@settings(max_examples=200)
@given(
    text=st.text(min_size=0, max_size=4000),
    max_tokens=st.integers(min_value=1, max_value=200),
)
def test_within_budget_returns_unchanged(text: str, max_tokens: int):
    """原文本已在预算内时原样返回。"""
    counter = HeuristicTokenCounter()
    result = truncate_to_tokens(text, max_tokens, counter, note=_NOTE)
    if counter.count(text) <= max_tokens:
        assert result == text


@settings(max_examples=200)
@given(
    text=st.text(min_size=0, max_size=4000),
    max_tokens=st.integers(min_value=1, max_value=200),
)
def test_truncation_preserves_marker(text: str, max_tokens: int):
    """真正发生截断（原文超预算）时，结果保留截断标记。"""
    counter = HeuristicTokenCounter()
    result = truncate_to_tokens(text, max_tokens, counter, note=_NOTE)
    if counter.count(text) > max_tokens:
        assert result.endswith(_NOTE)
