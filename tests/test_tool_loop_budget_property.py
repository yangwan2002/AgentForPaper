"""Property 7 性质测试：上下文预算（Req 8.1 / 8.3）。

**Validates: Requirements 8.1, 8.3**

形式化：``run_tool_loop`` 每轮（在 for 循环内）调用 LLM 前，传入的消息列表满足

    ``counter.count_messages(messages) <= context_token_budget``

「单条不可分割消息除外」——即当系统提示与最近 ``keep_recent_turns`` 轮原文本身
已超出预算时，允许越界（历史已达不可压缩下限）。本测试用「越界则必然不可再压缩」
来精确刻画这一例外：若某一轮传给 LLM 的消息超预算，则对其再做一次
``compact_history``（使用一个极短摘要器）仍无法压回预算内——证明越界确实只源于
不可分割消息，而非循环漏掉了应有的压缩。

生成策略：用 `hypothesis` 随机化预算、保留轮数、单条工具结果上限、最大轮数与
工具结果体量，覆盖「压缩后落入预算」与「单条消息天然越界」两类情形。计数器固定
为 `HeuristicTokenCounter` 以保证确定性。摘要器为不依赖 LLM 的平凡实现。
"""

from __future__ import annotations

from dataclasses import replace

from hypothesis import given, settings
from hypothesis import strategies as st

from paper_agent.agents.tool_loop import (
    ToolLoopConfig,
    compact_history,
    run_tool_loop,
)
from paper_agent.context.tokenizer import HeuristicTokenCounter
from paper_agent.providers.llm.base import LLMResponse, Message, ToolCall
from paper_agent.tools.registry import ToolRegistry

# 平凡摘要器：返回固定短串，既不依赖 LLM 又能让压缩真正缩小历史。
_SHORT = "早前要点"


def _short_summarizer(_text: str) -> str:
    return _SHORT


class _RecordingLLM:
    """确定性 fake LLM：

    - 带 `tools` 的调用 = 工具循环「每轮」对 LLM 的调用：记录此刻的消息快照，
      并请求一次工具调用（驱动历史增长以触发压缩）。
    - 不带 `tools` 的调用 = 历史摘要器或强制收尾：返回固定短正文，不请求工具。
    """

    def __init__(self) -> None:
        self.snapshots: list[list[Message]] = []
        self._round = 0

    def complete(self, messages: list[Message], **opts) -> LLMResponse:
        if "tools" in opts:
            # 记录「每轮调用 LLM 前」的消息列表（拷贝，避免被后续原地修改影响）。
            self.snapshots.append(list(messages))
            self._round += 1
            return LLMResponse(
                content="继续",
                tool_calls=[
                    ToolCall(id=f"c{self._round}", name="grow", arguments={})
                ],
            )
        # 摘要 / 强制收尾：短正文，无工具调用。
        return LLMResponse(content=_SHORT)


@settings(max_examples=150, deadline=None)
@given(
    context_token_budget=st.integers(min_value=20, max_value=300),
    keep_recent_turns=st.integers(min_value=1, max_value=3),
    max_tool_result_tokens=st.integers(min_value=100, max_value=400),
    max_iters=st.integers(min_value=3, max_value=8),
    payload_chars=st.integers(min_value=0, max_value=3000),
    n_system=st.integers(min_value=1, max_value=3),
)
def test_each_round_respects_context_budget(
    context_token_budget: int,
    keep_recent_turns: int,
    max_tool_result_tokens: int,
    max_iters: int,
    payload_chars: int,
    n_system: int,
):
    """每轮调用 LLM 前，消息 token ≤ 预算（不可分割消息除外）。"""
    counter = HeuristicTokenCounter()
    config = ToolLoopConfig(
        max_iters=max_iters,
        context_token_budget=context_token_budget,
        max_tool_result_tokens=max_tool_result_tokens,
        keep_recent_turns=keep_recent_turns,
    )

    # 每次工具调用回灌一段（可能很长的）结果，驱动历史增长。
    payload = "数据" * payload_chars

    registry = ToolRegistry()
    registry.register(
        "grow", "返回一段文本", lambda **_: payload,
        {"type": "object", "properties": {}},
    )

    llm = _RecordingLLM()
    messages: list[Message] = [
        Message(role="system", content=f"系统提示{i}：保持上下文有界。")
        for i in range(n_system)
    ]
    messages.append(Message(role="user", content="开始写作并按需检索。"))

    run_tool_loop(llm, messages, registry, counter=counter, config=config)

    # 至少发生过一轮带工具的 LLM 调用。
    assert llm.snapshots

    for snapshot in llm.snapshots:
        count = counter.count_messages(snapshot)
        if count <= context_token_budget:
            continue
        # 越界只允许源于不可分割消息：对该快照再压一次仍压不回预算内。
        recompacted = compact_history(
            snapshot, counter, config, _short_summarizer
        )
        assert counter.count_messages(recompacted) > context_token_budget, (
            "超预算的消息列表本可被进一步压缩，工具循环漏掉了应有的历史压缩"
        )


@settings(max_examples=200, deadline=None)
@given(
    context_token_budget=st.integers(min_value=10, max_value=300),
    keep_recent_turns=st.integers(min_value=1, max_value=3),
    n_system=st.integers(min_value=0, max_value=3),
    body_sizes=st.lists(
        st.integers(min_value=0, max_value=600), min_size=0, max_size=12
    ),
)
def test_compact_history_preserves_system_and_recent(
    context_token_budget: int,
    keep_recent_turns: int,
    n_system: int,
    body_sizes: list[int],
):
    """compact_history 保留全部系统提示与最近 keep_recent_turns 轮原文，旧轮折叠为单条摘要。"""
    counter = HeuristicTokenCounter()
    config = ToolLoopConfig(
        context_token_budget=context_token_budget,
        keep_recent_turns=keep_recent_turns,
    )

    system_msgs = [
        Message(role="system", content=f"sys{i}") for i in range(n_system)
    ]
    body_msgs = [
        Message(
            role=("assistant" if i % 2 == 0 else "tool"),
            content=("内容" * size),
        )
        for i, size in enumerate(body_sizes)
    ]
    messages = system_msgs + body_msgs

    result = compact_history(messages, counter, config, _short_summarizer)

    keep = keep_recent_turns * 2
    recent = body_msgs[len(body_msgs) - keep:] if keep else []
    old = body_msgs[: len(body_msgs) - len(recent)]

    if not old:
        # 无可折叠旧轮：原样返回。
        assert result is messages
        return

    # 全部原始系统提示被保留。
    res_system = [m for m in result if m.role == "system"]
    for sm in system_msgs:
        assert sm in res_system

    # 最近 keep 轮原文按序保留在尾部、内容不变。
    res_body = [m for m in result if m.role != "system"]
    assert res_body == recent

    # 旧轮被折叠为「单条」摘要消息（system 角色），非系统消息条数不增。
    assert len(res_system) == len(system_msgs) + 1
    assert any(m.content.startswith("[早前对话摘要]") for m in res_system)
    assert len(res_body) <= len(body_msgs)
