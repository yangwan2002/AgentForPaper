"""read_section 分页读取测试：offset/limit 窗口 + 尾部续读提示 + 向后兼容。"""

from __future__ import annotations

import copy

from paper_agent.agent_platform.guardrail_gate import GuardrailGate
from paper_agent.agent_platform.models import AgentSession, WritingTask
from paper_agent.agent_platform.tools.context import ToolContext
from paper_agent.agent_platform.tools.read import register_read_section
from paper_agent.elicitation import AutoElicitor
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


def _ctx(content: str):
    ws = PaperWorkspace(workspace_id="w1", input_mode=InputMode.DRAFT_REVISION)
    ws.outline = [OutlineNode(section_id="s1", title="方法", order=0)]
    ws.section_drafts = {"s1": SectionDraft(section_id="s1", title="方法", content=content)}
    repo = WorkspaceRepository(_MemStore())
    repo.create(ws)
    session = AgentSession(session_id="w1", workspace=ws, task=WritingTask("t"))
    return ToolContext(
        session=session, repo=repo, gate=GuardrailGate(),
        elicitor=AutoElicitor(), output_dir="out",
    )


def _registry(content: str):
    ctx = _ctx(content)
    registry = ToolRegistry()
    register_read_section(registry, ctx)
    return registry


def test_short_section_full_no_footer():
    """短章节一次读全，无续读提示。"""
    registry = _registry("这是一段不长的方法正文。")
    out = registry.call("read_section", section_id="s1")
    assert "这是一段不长的方法正文。" in out
    assert "未读" not in out


def test_backward_compatible_only_section_id():
    """只传 section_id 仍可用（默认从头读默认窗口）。"""
    registry = _registry("内容 A。")
    out = registry.call("read_section", section_id="s1")
    assert "内容 A。" in out


def test_long_section_paginates_with_next_offset():
    """超长章节首页带续读提示，且提示的 offset 能读到尾部。"""
    # 构造一段远超默认窗口（4000 字符）的正文，尾部放一个可检索的哨兵串。
    body = "甲" * 5000 + "END_SENTINEL_乙"
    registry = _registry(body)

    page1 = registry.call("read_section", section_id="s1", limit=4000)
    assert "共" in page1 and "未读" in page1
    assert "END_SENTINEL_乙" not in page1  # 尾部尚未读到
    # 从提示里解析 next offset。
    assert "offset=4000" in page1

    page2 = registry.call("read_section", section_id="s1", offset=4000, limit=4000)
    assert "END_SENTINEL_乙" in page2  # 续读读到尾部
    assert "未读" not in page2  # 已到末尾


def test_offset_beyond_end_reports_no_more():
    registry = _registry("短内容。")
    out = registry.call("read_section", section_id="s1", offset=9999)
    assert "超出末尾" in out


def test_limit_is_clamped():
    """limit 超上限被规整，不报错、正常返回窗口。"""
    body = "字" * 100
    registry = _registry(body)
    out = registry.call("read_section", section_id="s1", limit=999999)
    assert "字" in out
    assert "未读" not in out  # 100 字符 < 上限，一次读全


def test_missing_section_returns_error():
    registry = _registry("x")
    out = registry.call("read_section", section_id="nope")
    assert "不存在" in out
