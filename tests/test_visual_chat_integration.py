"""visual-layout-acceptance 波5：ChatController 接入测试。

覆盖 Property 2（关=零副作用）、Property 5（不盲跑）、确定性触发接入、诚实上报拼接。
用 fake gate（不依赖真实渲染/vision），验证 ChatController 的触发与拼接逻辑。
"""

from __future__ import annotations

import copy

from paper_agent.agent_platform.chat import ChatController
from paper_agent.agent_platform.guardrail_gate import GuardrailGate
from paper_agent.agent_platform.models import AgentSession, WritingTask
from paper_agent.agent_platform.task_agent import TaskAgent
from paper_agent.agent_platform.visual.gate import VisualAcceptanceOutcome
from paper_agent.providers.llm.base import LLMResponse
from paper_agent.tools.registry import ToolRegistry
from paper_agent.workspace.models import InputMode, PaperWorkspace
from paper_agent.workspace.repository import WorkspaceRepository


class _MemStore:
    def __init__(self):
        self._data = {}

    def load(self, wid):
        raw = self._data.get(wid)
        return PaperWorkspace.from_dict(raw) if raw else None

    def save(self, ws):
        self._data[ws.workspace_id] = copy.deepcopy(ws.to_dict())


class _AgentLLM:
    def complete(self, messages, **opts):
        return LLMResponse(content="ok")


class _SpyGate:
    """记录是否被调用的假视觉闸。"""

    def __init__(self, outcome):
        self.calls = 0
        self._outcome = outcome
        self.last_docx = None
        self.last_requirement = None

    def evaluate(self, docx_path, layout_requirement, **kw):
        self.calls += 1
        self.last_docx = docx_path
        self.last_requirement = layout_requirement
        return self._outcome


def _controller(tmp_path, gate, *, enabled):
    ws = PaperWorkspace(workspace_id="w1", input_mode=InputMode.DRAFT_REVISION)
    repo = WorkspaceRepository(_MemStore())
    repo.create(ws)
    session = AgentSession(session_id="w1", workspace=ws, task=WritingTask(""))
    agent = TaskAgent(_AgentLLM(), ToolRegistry())
    return ChatController(
        agent, session, repo, output_dir=str(tmp_path), enable_acceptance=False,
        visual_gate=gate, visual_enabled=enabled,
    ), session


def _entries_layout(tmp_path):
    docx = tmp_path / "out.docx"; docx.write_bytes(b"x")
    return [{"kind": "tool_call", "name": "convert_document", "files": [str(docx)]}]


def test_disabled_gate_zero_side_effects(tmp_path):
    gate = _SpyGate(VisualAcceptanceOutcome(ran=True, satisfied=True))
    ctrl, _ = _controller(tmp_path, gate, enabled=False)   # 主开关关
    note = ctrl._append_visual_note("", "图跨双栏", _entries_layout(tmp_path))
    assert note == ""
    assert gate.calls == 0                                  # Property 2：零调用


def test_layout_op_triggers_gate(tmp_path):
    out = VisualAcceptanceOutcome(ran=True, satisfied=True)
    gate = _SpyGate(out)
    ctrl, _ = _controller(tmp_path, gate, enabled=True)
    note = ctrl._append_visual_note("", "把图1改成跨双栏", _entries_layout(tmp_path))
    assert gate.calls == 1
    assert gate.last_requirement == "把图1改成跨双栏"
    assert "✓ 视觉版面校验通过" in note


def test_pure_polish_does_not_trigger(tmp_path):
    gate = _SpyGate(VisualAcceptanceOutcome(ran=True, satisfied=True))
    ctrl, _ = _controller(tmp_path, gate, enabled=True)
    docx = tmp_path / "out.docx"; docx.write_bytes(b"x")
    # 有 docx 产物，但本轮是纯润色（无版面操作、无 check_layout）→ 不触发（Property 5）。
    entries = [{"name": "rewrite_section", "notes": ["润色语言"], "files": [str(docx)]}]
    note = ctrl._append_visual_note("", "润色一下", entries)
    assert gate.calls == 0
    assert note == ""


def test_agent_requested_check_layout_triggers(tmp_path):
    gate = _SpyGate(VisualAcceptanceOutcome(ran=True, satisfied=False, defects=["图上方空白"]))
    ctrl, _ = _controller(tmp_path, gate, enabled=True)
    docx = tmp_path / "out.docx"; docx.write_bytes(b"x")
    # 非版面工具，但 agent 主动调了 check_layout → 触发。
    entries = [
        {"name": "rewrite_section", "files": [str(docx)]},
        {"name": "check_layout"},
    ]
    note = ctrl._append_visual_note("", "看看版面", entries)
    assert gate.calls == 1
    assert "未达成" in note and "图上方空白" in note        # 诚实上报缺陷


def test_no_docx_product_no_trigger(tmp_path):
    gate = _SpyGate(VisualAcceptanceOutcome(ran=True, satisfied=True))
    ctrl, _ = _controller(tmp_path, gate, enabled=True)
    entries = [{"name": "convert_document", "files": ["out.tex"]}]  # 非 docx 产物
    note = ctrl._append_visual_note("", "转 latex", entries)
    assert gate.calls == 0


def test_skip_outcome_appends_nothing(tmp_path):
    gate = _SpyGate(VisualAcceptanceOutcome(ran=False, skip_reason="无渲染后端"))
    ctrl, _ = _controller(tmp_path, gate, enabled=True)
    note = ctrl._append_visual_note("原验收提示", "图跨双栏", _entries_layout(tmp_path))
    assert gate.calls == 1
    assert note == "原验收提示"                             # skip 安静，不追加噪音


def test_records_visual_acceptance(tmp_path):
    gate = _SpyGate(VisualAcceptanceOutcome(ran=True, satisfied=True, backend="word_com"))
    ctrl, session = _controller(tmp_path, gate, enabled=True)
    ctrl._append_visual_note("", "图跨双栏", _entries_layout(tmp_path))
    recs = [e for e in session.transcript if e.get("kind") == "visual_acceptance"]
    assert recs and recs[-1].get("satisfied") is True
