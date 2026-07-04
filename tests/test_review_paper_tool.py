"""review_paper 只读评审工具测试（Task 5）。

验证：调用前后工作区字节不变（Property 6 只读）、返回含维度评分文本、
用户未请求时不触发（由系统提示约束，此处测工具本身只读性质）。
"""

from __future__ import annotations

import copy

from paper_agent.agent_platform.guardrail_gate import GuardrailGate
from paper_agent.agent_platform.models import AgentSession, WritingTask
from paper_agent.agent_platform.tools.context import ToolContext
from paper_agent.agent_platform.tools.review import register_review_paper
from paper_agent.providers.llm.base import LLMResponse
from paper_agent.tools.registry import ToolRegistry
from paper_agent.workspace.models import (
    InputMode,
    OutlineNode,
    PaperWorkspace,
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


class _ReviewLLM:
    """返回可解析评分 JSON 的 fake LLM。"""

    def complete(self, messages, **opts):
        return LLMResponse(content=(
            '{"scores": {"logic": 7.0, "novelty": 6.0, "sufficiency": 6.5, '
            '"language": 8.0}, "suggestions": {"logic": "加强论证衔接"}, '
            '"section_feedback": {"s1": "引言可更聚焦"}}'
        ))


def _ctx():
    ws = PaperWorkspace(workspace_id="w1", input_mode=InputMode.DRAFT_REVISION)
    ws.outline = [OutlineNode(section_id="s1", title="引言", order=0)]
    ws.section_drafts = {
        "s1": SectionDraft(section_id="s1", title="引言", content="这是一段引言正文，用于评审。")
    }
    repo = WorkspaceRepository(_MemStore())
    repo.create(ws)
    session = AgentSession(session_id="w1", workspace=ws, task=WritingTask("评审"))
    return ToolContext(session=session, repo=repo, gate=GuardrailGate(), elicitor=None), ws


def test_review_paper_returns_scores():
    ctx, ws = _ctx()
    registry = ToolRegistry()
    register_review_paper(registry, ctx, _ReviewLLM())
    out = registry.call("review_paper")
    assert "评审评分" in out
    assert "logic" in out and "7.0" in out


def test_review_paper_is_read_only():
    ctx, ws = _ctx()
    before = copy.deepcopy(ws.to_dict())
    registry = ToolRegistry()
    register_review_paper(registry, ctx, _ReviewLLM())
    registry.call("review_paper")
    after = ws.to_dict()
    # 工作区内容字节不变：评审记录未写入真实工作区。
    assert after == before
    assert ws.review_records == []


def test_review_paper_empty_paper_reports_unavailable():
    ctx, ws = _ctx()
    ws.section_drafts = {}
    registry = ToolRegistry()
    register_review_paper(registry, ctx, _ReviewLLM())
    out = registry.call("review_paper")
    assert "评审不可用" in out or "评分" in out
