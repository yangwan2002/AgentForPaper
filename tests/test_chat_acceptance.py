"""chat 路径接入收尾验收测试（P0-1）。

验证：本轮产生导出产物时，ChatController 对成品跑确定性验收，并把结论（通过/未解决）
经 acceptance_note 暴露；无导出产物的轮次不触发验收（不打扰中间步骤）。
"""

from __future__ import annotations

import copy

from paper_agent.agent_platform.chat import ChatController
from paper_agent.agent_platform.guardrail_gate import GuardrailGate
from paper_agent.agent_platform.models import AgentSession, WritingTask
from paper_agent.agent_platform.task_agent import TaskAgent
from paper_agent.agent_platform.tools.context import ToolContext
from paper_agent.agent_platform.tools.export_tool import register_export_paper
from paper_agent.elicitation import AutoElicitor
from paper_agent.providers.llm.base import LLMResponse, ToolCall
from paper_agent.tools.registry import ToolRegistry
from paper_agent.workspace.models import (
    InputMode,
    OutlineNode,
    OutputFormat,
    PaperWorkspace,
    ReferenceEntry,
    SectionDraft,
)
from paper_agent.workspace.repository import WorkspaceRepository


class _MemStore:
    def __init__(self):
        self._data = {}

    def load(self, wid):
        raw = self._data.get(wid)
        return PaperWorkspace.from_dict(raw) if raw else None

    def save(self, ws):
        self._data[ws.workspace_id] = copy.deepcopy(ws.to_dict())


class _ScriptedLLM:
    def __init__(self, script):
        self._script = list(script)
        self._i = 0

    def complete(self, messages, **opts):
        if self._i < len(self._script):
            resp = self._script[self._i]
            self._i += 1
            return resp
        return LLMResponse(content="完成。")


def _controller(tmp_path, *, dangling=False):
    ws = PaperWorkspace(
        workspace_id="w1", input_mode=InputMode.DRAFT_REVISION,
        output_format=OutputFormat.MARKDOWN,
    )
    ws.outline = [OutlineNode(section_id="s1", title="Intro", order=0)]
    ws.verified_references = [
        ReferenceEntry(id="1", title="A", authors=["X"], year=2021, source_id="d1", verified=True)
    ]
    content = "正常中文正文，引用 [1]。" + (" 还有 [99]。" if dangling else "")
    ws.section_drafts = {"s1": SectionDraft(section_id="s1", title="Intro", content=content)}
    repo = WorkspaceRepository(_MemStore())
    repo.create(ws)
    session = AgentSession(session_id="w1", workspace=ws, task=WritingTask(""))
    ctx = ToolContext(
        session=session, repo=repo, gate=GuardrailGate(),
        elicitor=AutoElicitor(), output_dir=str(tmp_path),
    )
    registry = ToolRegistry()
    register_export_paper(registry, ctx)
    # 主循环：调 export_paper 再收尾。
    script = [
        LLMResponse(content="", tool_calls=[ToolCall(
            id="e1", name="export_paper", arguments={"format": "markdown"})]),
        LLMResponse(content="已导出。"),
    ]
    agent = TaskAgent(_ScriptedLLM(script), registry)
    return ChatController(
        agent, session, repo, output_dir=str(tmp_path),
        enable_acceptance=True, acceptance_max_heal_rounds=1,
    ), ws


def test_chat_acceptance_pass_after_export(tmp_path):
    controller, ws = _controller(tmp_path, dangling=False)
    turn = controller.send("帮我导出为 markdown")
    assert "export_paper" in turn.tool_calls
    assert turn.acceptance_note  # 触发了验收
    assert "✓" in turn.acceptance_note  # 干净稿通过


def test_chat_acceptance_reports_dangling(tmp_path):
    controller, ws = _controller(tmp_path, dangling=True)
    turn = controller.send("帮我导出为 markdown")
    assert turn.acceptance_note
    assert "未解决" in turn.acceptance_note
    assert "99" in turn.acceptance_note  # 悬空引用被如实上报


def test_chat_no_export_no_acceptance(tmp_path):
    """无导出产物的轮次不触发验收（不打扰中间步骤）。"""
    ws = PaperWorkspace(workspace_id="w1", input_mode=InputMode.DRAFT_REVISION)
    ws.outline = [OutlineNode(section_id="s1", title="Intro", order=0)]
    ws.section_drafts = {"s1": SectionDraft(section_id="s1", title="Intro", content="内容")}
    repo = WorkspaceRepository(_MemStore())
    repo.create(ws)
    session = AgentSession(session_id="w1", workspace=ws, task=WritingTask(""))
    agent = TaskAgent(_ScriptedLLM([LLMResponse(content="好的。")]), ToolRegistry())
    controller = ChatController(
        agent, session, repo, output_dir=str(tmp_path), enable_acceptance=True
    )
    turn = controller.send("帮我看看引言写得怎么样")
    assert turn.acceptance_note == ""
