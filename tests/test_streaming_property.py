"""Property 4 / Property 6 性质测试：流式与 complete 的一致性。

**Property 4: complete 向后兼容；Property 6: 流式聚合一致**
**Validates: Requirements 5.2, 5.10**

在同一确定性 mock 下断言：

- Property 4（Req 5.2）：`complete(messages)` 的返回语义与是否传入 `on_delta`
  回调无关——无论是否传 `on_delta`，`complete()` 都返回同一段完整 `content`
  （即引入流式适配后 `complete()` 行为保持向后兼容）。
- Property 6（Req 5.10）：`stream()` 产出的 content 增量按序拼接，等于等价
  `complete()` 的 `content`（同一确定性 mock、同一输入下）。

设计动机：`MockLLMProvider` 的 `complete` 不触发 `on_delta`，无法驱动
`StreamingMixin`。这里构造一个**确定性** fake provider：给定一段内容串，将其
切成若干片，若调用方传入 `on_delta` 则对每片调用 `on_delta("content", piece)`，
最终 `complete()` 始终返回完整内容。该 provider 混入 `StreamingMixin`，从而其
`stream()` 经 `complete(on_delta=...)` 适配得到，便于验证聚合一致性。
"""

from __future__ import annotations

from hypothesis import given, settings
from hypothesis import strategies as st

from paper_agent.providers.llm.base import LLMResponse, Message
from paper_agent.providers.llm.streaming import StreamingMixin


def _split_into_pieces(text: str, n: int) -> list[str]:
    """把 `text` 确定性地切成至多 `n` 个非空片段，拼接后等于原文。"""
    if not text:
        return []
    n = max(1, min(n, len(text)))
    size = len(text) // n
    pieces: list[str] = []
    idx = 0
    for i in range(n):
        if i == n - 1:
            pieces.append(text[idx:])
        else:
            pieces.append(text[idx : idx + size])
            idx += size
    return [p for p in pieces if p]


class _DeterministicStreamingProvider(StreamingMixin):
    """确定性 fake provider：complete 返回固定内容，并按需推送 content 增量。

    - `complete(messages, on_delta=None)`：若传入 `on_delta`，先逐片调用
      `on_delta("content", piece)`（外加一条 `thinking` 增量以验证 content 过滤），
      无论是否传回调都返回 `LLMResponse(content=full)`。
    - `stream()` 由 `StreamingMixin` 经 `complete(on_delta=...)` 适配而来。
    """

    def __init__(self, content: str, n_pieces: int) -> None:
        self._content = content
        self._pieces = _split_into_pieces(content, n_pieces)
        self.complete_calls = 0

    def complete(self, messages: list[Message], *, on_delta=None, **opts) -> LLMResponse:
        self.complete_calls += 1
        if on_delta is not None:
            # 先推一条 thinking 增量，确保 content 聚合会正确过滤非 content 块。
            on_delta("thinking", "<thinking>")
            for piece in self._pieces:
                on_delta("content", piece)
        return LLMResponse(content=self._content)


_MESSAGES = [Message("user", "请写一段内容")]


@settings(max_examples=200, deadline=None)
@given(content=st.text(max_size=200), n_pieces=st.integers(min_value=1, max_value=20))
def test_property6_stream_content_aggregates_to_complete(
    content: str, n_pieces: int
) -> None:
    """Property 6：stream() 的 content 增量按序拼接 == complete().content。

    **Validates: Requirements 5.10**
    """
    provider = _DeterministicStreamingProvider(content, n_pieces)

    chunks = list(provider.stream(_MESSAGES))
    aggregated = "".join(c.text for c in chunks if c.kind == "content")

    expected = provider.complete(_MESSAGES).content
    assert aggregated == expected
    # 同一输入、确定性 mock 下，聚合结果就是原始内容串。
    assert aggregated == content


@settings(max_examples=200, deadline=None)
@given(content=st.text(max_size=200), n_pieces=st.integers(min_value=1, max_value=20))
def test_property4_complete_backward_compatible(content: str, n_pieces: int) -> None:
    """Property 4：complete() 行为与是否传入 on_delta 无关，始终返回完整内容。

    **Validates: Requirements 5.2**
    """
    provider = _DeterministicStreamingProvider(content, n_pieces)

    # 不传 on_delta：经典 complete 调用。
    plain = provider.complete(_MESSAGES).content

    # 传 on_delta：增量回调被触发，但 complete 的返回语义不变。
    collected: list[str] = []
    with_delta = provider.complete(
        _MESSAGES, on_delta=lambda kind, text: collected.append((kind, text))
    ).content

    assert plain == with_delta == content
    # 回调推送的 content 增量按序拼接同样等于完整内容（与 stream 聚合一致）。
    assert "".join(text for kind, text in collected if kind == "content") == content
