"""流式 REPL 下工作流回复必须显示（回归：选「开始」后结果被吞掉像任务终止）。

根因：流式模式假设回复由 LLM 实时流式打印，但意图路由命中固定任务时回复由工作流产生、
不走流式，被 _one_turn 吞掉。修复后：非流式（streamed=False）的回复在流式模式下显式打印。
"""

from __future__ import annotations

from paper_agent.agent_platform.chat import ChatTurn, _one_turn


class _StubController:
    """返回预置 ChatTurn 的假控制器（只测 REPL 显示逻辑）。"""

    def __init__(self, turn):
        self._turn = turn
        self.sent = []

    def send(self, text):
        self.sent.append(text)
        return self._turn


def _collect():
    lines = []
    return lines, (lambda s: lines.append(s))


def test_streaming_prints_workflow_reply():
    """工作流回复（streamed=False）在流式模式下必须被打印。"""
    turn = ChatTurn(
        reply="已直转为 docx：output/paper_converted.docx。", tool_calls=["convert_format"],
        made_progress=True, streamed=False,
    )
    lines, out = _collect()
    _one_turn(_StubController(turn), "1", out, streaming=True)
    assert any("已直转为 docx" in ln for ln in lines)


def test_streaming_prints_cancel_reply():
    """取消/澄清等非流式回复也须显示。"""
    turn = ChatTurn(reply="已取消。请重新描述你的需求。", streamed=False)
    lines, out = _collect()
    _one_turn(_StubController(turn), "3", out, streaming=True)
    assert any("已取消" in ln for ln in lines)


def test_streaming_does_not_double_print_converse():
    """converse 回复（streamed=True）已实时流式，不应再重复整段打印。"""
    turn = ChatTurn(reply="这是一段已经流式输出过的答复。", streamed=True)
    lines, out = _collect()
    _one_turn(_StubController(turn), "你好", out, streaming=True)
    # 不重复打印整段（避免与实时流式重复）。
    assert not any("这是一段已经流式输出过的答复。" == ln for ln in lines)


def test_nonstreaming_prints_reply_as_before():
    """非流式模式行为不变：整段打印回复。"""
    turn = ChatTurn(reply="常规答复。", streamed=True)
    lines, out = _collect()
    _one_turn(_StubController(turn), "hi", out, streaming=False)
    assert any("常规答复。" in ln for ln in lines)
