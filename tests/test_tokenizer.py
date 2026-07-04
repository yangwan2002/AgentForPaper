"""分词器单元测试（Req 7.2 / 7.3）。

覆盖：
- `HeuristicTokenCounter`：空串计 0、非空至少 1、约每 2 字符 1 token 的向上取整、
  `count_messages` 累加、计数恒非负。
- `build_token_counter`：无论 `tiktoken` 是否安装都返回可用的 `TokenCounter`
  （本环境通常未安装 → 期望 `HeuristicTokenCounter`），并满足运行期协议检查。
- `TiktokenCounter`：未知模型名仍回退 `cl100k_base`，计数非负且不抛异常
  （`tiktoken` 缺失时跳过）。
"""

from __future__ import annotations

import math

import pytest

from paper_agent.context.tokenizer import (
    HeuristicTokenCounter,
    TiktokenCounter,
    TokenCounter,
    build_token_counter,
)
from paper_agent.providers.llm.base import Message, ToolCall


# ---------------------------------------------------------------------------
# HeuristicTokenCounter
# ---------------------------------------------------------------------------


def test_heuristic_empty_string_is_zero():
    """空文本计 0。"""
    counter = HeuristicTokenCounter()
    assert counter.count("") == 0


def test_heuristic_nonempty_at_least_one():
    """非空文本至少计 1 token（即便只有 1 个字符）。"""
    counter = HeuristicTokenCounter()
    assert counter.count("a") >= 1
    # 单字符：ceil(1/2)=1。
    assert counter.count("a") == 1


def test_heuristic_two_chars_per_token_ceil():
    """约每 2 字符 1 token、向上取整。"""
    counter = HeuristicTokenCounter(chars_per_token=2)
    assert counter.count("ab") == 1          # ceil(2/2)
    assert counter.count("abc") == 2         # ceil(3/2)
    assert counter.count("abcd") == 2        # ceil(4/2)
    assert counter.count("abcde") == 3       # ceil(5/2)


def test_heuristic_matches_ceil_formula_for_many_lengths():
    """对多种长度满足 max(1, ceil(len/2)) 口径。"""
    counter = HeuristicTokenCounter(chars_per_token=2)
    for n in range(1, 50):
        text = "x" * n
        assert counter.count(text) == max(1, math.ceil(n / 2))


def test_heuristic_always_non_negative():
    """计数恒非负。"""
    counter = HeuristicTokenCounter()
    for text in ["", "a", "hello world", "中文字符测试", "\n\t  "]:
        assert counter.count(text) >= 0


def test_heuristic_count_messages_sums():
    """count_messages 等于各消息文本计数之和。"""
    counter = HeuristicTokenCounter()
    messages = [
        Message(role="system", content="你是助手"),
        Message(role="user", content="请总结这段文本"),
        Message(role="assistant", content="好的"),
    ]
    expected = sum(
        counter.count("\n".join([m.role, m.content])) for m in messages
    )
    assert counter.count_messages(messages) == expected


def test_heuristic_count_messages_empty_list_is_zero():
    """空消息列表计 0。"""
    counter = HeuristicTokenCounter()
    assert counter.count_messages([]) == 0


def test_heuristic_count_messages_includes_tool_calls():
    """assistant 携带的 tool_calls（名称 + 参数）也计入计数。"""
    counter = HeuristicTokenCounter()
    plain = Message(role="assistant", content="调用工具")
    with_calls = Message(
        role="assistant",
        content="调用工具",
        tool_calls=[ToolCall(id="1", name="search", arguments={"q": "x"})],
    )
    # 携带 tool_calls 的消息文本更长 → 计数不小于不带的。
    assert counter.count_messages([with_calls]) >= counter.count_messages([plain])


# ---------------------------------------------------------------------------
# build_token_counter
# ---------------------------------------------------------------------------


def _tiktoken_installed() -> bool:
    try:
        import tiktoken  # noqa: F401
    except Exception:
        return False
    return True


def test_build_token_counter_returns_working_counter():
    """无论 tiktoken 是否安装，都返回可用的 TokenCounter。"""
    counter = build_token_counter(model="gpt-4o-mini")
    # 运行期可检查协议。
    assert isinstance(counter, TokenCounter)
    # 真正可用：空串 0、非空非负。
    assert counter.count("") == 0
    assert counter.count("hello") >= 0
    assert counter.count_messages([Message(role="user", content="hi")]) >= 0


def test_build_token_counter_type_depends_on_tiktoken():
    """tiktoken 缺失时回退 HeuristicTokenCounter；安装时使用 TiktokenCounter。"""
    counter = build_token_counter(model="gpt-4o-mini")
    if _tiktoken_installed():
        assert isinstance(counter, TiktokenCounter)
    else:
        assert isinstance(counter, HeuristicTokenCounter)


def test_build_token_counter_does_not_raise_on_empty_model():
    """空模型名也能构造出可用计数器，不抛异常。"""
    counter = build_token_counter(model="")
    assert isinstance(counter, TokenCounter)
    assert counter.count("abc") >= 0


# ---------------------------------------------------------------------------
# TiktokenCounter 回退（tiktoken 缺失时跳过）
# ---------------------------------------------------------------------------


def test_tiktoken_unknown_model_falls_back_and_counts():
    """未知模型名仍回退 cl100k_base，计数非负且不抛异常。"""
    pytest.importorskip("tiktoken")
    counter = TiktokenCounter(model="totally-unknown-model-xyz-123")
    assert counter.count("") == 0
    assert counter.count("hello world") >= 1
    assert counter.count_messages([Message(role="user", content="hi")]) >= 0


def test_tiktoken_isinstance_token_counter():
    """TiktokenCounter 满足运行期协议检查。"""
    pytest.importorskip("tiktoken")
    counter = TiktokenCounter(model="gpt-4o-mini")
    assert isinstance(counter, TokenCounter)
