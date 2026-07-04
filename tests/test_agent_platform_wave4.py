"""波次 4 工具测试（任务 6 run_full_pipeline / 任务 7 set_typesetting / 任务 8 外部工具）。"""

from __future__ import annotations

import copy

import pytest

from paper_agent.agent_platform.external_tools import register_external_tools
from paper_agent.agent_platform.guardrail_gate import GuardrailGate
from paper_agent.agent_platform.models import (
    CHANGE_CONTENT,
    AgentSession,
    ProposedChange,
    ToolSpec,
    Typesetting,
    WritingTask,
)
from paper_agent.agent_platform.tools.context import ToolContext
from paper_agent.agent_platform.tools.full_pipeline import register_run_full_pipeline
from paper_agent.agent_platform.tools.typesetting_tool import register_set_typesetting
from paper_agent.elicitation import AutoElicitor
from paper_agent.tools.registry import ToolRegistry
from paper_agent.workspace.models import (
    InputMode,
    OutlineNode,
    OutputFormat,
    PaperWorkspace,
    SectionDraft,
)
from paper_agent.workspace.repository import WorkspaceRepository

docx = pytest.importorskip("docx")  # 排版测试需要 python-docx


class _MemStore:
    def __init__(self):
        self._data = {}

    def load(self, wid):
        raw = self._data.get(wid)
        return PaperWorkspace.from_dict(raw) if raw else None

    def save(self, ws):
        self._data[ws.workspace_id] = copy.deepcopy(ws.to_dict())


def _ws(fmt=OutputFormat.DOCX):
    ws = PaperWorkspace(workspace_id="w1", input_mode=InputMode.DRAFT_REVISION, output_format=fmt)
    ws.outline = [OutlineNode(section_id="intro", title="引言", order=0)]
    ws.section_drafts = {
        "intro": SectionDraft(section_id="intro", title="引言", content="这是一段足够长的正文内容用于导出测试。"),
    }
    return ws


def _ctx(ws=None, out="output"):
    ws = ws or _ws()
    repo = WorkspaceRepository(_MemStore())
    repo.create(ws)
    session = AgentSession(session_id="w1", workspace=ws, task=WritingTask("t"))
    return ToolContext(session=session, repo=repo, gate=GuardrailGate(), elicitor=AutoElicitor(), output_dir=out)


# --- 任务 6：run_full_pipeline ----------------------------------------------

class _FakeResult:
    def __init__(self, files):
        self.terminated_reason = "quality_met"
        self.submittable = True
        class _E:
            pass
        self.export = _E()
        self.export.files = files


def test_run_full_pipeline_reloads_workspace_and_reports():
    ctx = _ctx()

    def _runner(wid):
        # 模拟管线：在同一 store 上改工作区并落盘，返回结果。
        ws = ctx.repo.load(wid)
        ws.section_drafts["intro"].content = "管线重写后的引言。"
        ctx.repo.update(ws, lambda w: None)
        # 需要把改动落盘：直接 save 经 update
        ctx.repo.update(ws, lambda w: w.section_drafts.__setitem__(
            "intro", SectionDraft(section_id="intro", title="引言", content="管线重写后的引言。")))
        return _FakeResult(files=["output/w1.docx"])

    registry = ToolRegistry()
    register_run_full_pipeline(registry, ctx, _runner)
    out = registry.call("run_full_pipeline")
    assert "完整管线已运行" in out
    assert "quality_met" in out
    assert "output/w1.docx" in out
    # 工作区已回填最新内容。
    assert ctx.session.workspace.section_drafts["intro"].content == "管线重写后的引言。"


# --- 任务 7：set_typesetting -------------------------------------------------

def test_set_typesetting_records_spec_only(tmp_path):
    # set_typesetting 现在是纯设置：只记录规格、不产文件。
    ctx = _ctx(out=str(tmp_path))
    registry = ToolRegistry()
    register_set_typesetting(registry, ctx)
    out = registry.call(
        "set_typesetting", line_spacing=22, alignment="justify", first_line_indent="2ch"
    )
    assert "已记录排版规格" in out
    # 规格已记入 profile（导出时由 export_paper 套用）。
    assert ctx.session.workspace.profile.get("typesetting", {}).get("line_spacing") == 22.0
    # 未产生 docx 文件（不再自行导出）。
    assert not (tmp_path / "w1.docx").exists()


def test_set_typesetting_then_export_applies(tmp_path):
    # 记录规格后，export_paper 导出时自动套用两端对齐。
    from docx.enum.text import WD_ALIGN_PARAGRAPH

    from paper_agent.agent_platform.tools.export_tool import register_export_paper

    ctx = _ctx(out=str(tmp_path))
    registry = ToolRegistry()
    register_set_typesetting(registry, ctx)
    register_export_paper(registry, ctx)
    registry.call("set_typesetting", line_spacing=22, alignment="justify", first_line_indent="2ch")
    out = registry.call("export_paper", format="docx")
    assert "已套用已保存的排版规格" in out
    document = docx.Document(str(tmp_path / "w1.docx"))
    assert any(p.alignment == WD_ALIGN_PARAGRAPH.JUSTIFY for p in document.paragraphs)


def test_set_typesetting_empty_spec_noop():
    ctx = _ctx()
    registry = ToolRegistry()
    register_set_typesetting(registry, ctx)
    out = registry.call("set_typesetting")
    assert "未提供任何排版规格" in out


# --- 任务 8：外部工具 --------------------------------------------------------

class _FakeReadOnlyProvider:
    def discover(self):
        return [ToolSpec(name="draw_figure", description="画图", parameters_schema={
            "type": "object", "properties": {"kind": {"type": "string"}}, "required": []})]

    def invoke(self, name, **kwargs):
        return f"已画图：{kwargs.get('kind', 'default')}"


class _FakeWritingProvider:
    def discover(self):
        return [ToolSpec(name="inject_intro", description="外部写引言")]

    def invoke(self, name, **kwargs):
        def _mut(ws):
            ws.section_drafts["intro"].content = "外部工具写入的引言。"
        return [ProposedChange(mutation=_mut, kind=CHANGE_CONTENT, section_id="intro")]


class _FailingProvider:
    def discover(self):
        return [ToolSpec(name="flaky", description="会失败的外部工具")]

    def invoke(self, name, **kwargs):
        raise RuntimeError("外部服务不可用")


def test_external_readonly_tool_registered_and_callable():
    ctx = _ctx()
    registry = ToolRegistry()
    names = register_external_tools(registry, ctx, _FakeReadOnlyProvider())
    assert names == ["draw_figure"]
    out = registry.call("draw_figure", kind="bar")
    assert "已画图：bar" in out


def test_external_writing_tool_goes_through_guardrail_and_write_path():
    ctx = _ctx()
    registry = ToolRegistry()
    register_external_tools(registry, ctx, _FakeWritingProvider())
    out = registry.call("inject_intro")
    assert "通过护栏并落盘" in out
    assert ctx.session.workspace.section_drafts["intro"].content == "外部工具写入的引言。"
    assert ctx.repo.load("w1").section_drafts["intro"].content == "外部工具写入的引言。"


def test_external_writing_tool_rejected_by_guardrail():
    class _Q:
        def check(self, ws):
            class R:
                issues = [{"type": "placeholder", "severity": "high", "section_id": "intro", "message": "外部内容含占位"}]
            return R()

    ws = _ws()
    repo = WorkspaceRepository(_MemStore())
    repo.create(ws)
    session = AgentSession(session_id="w1", workspace=ws, task=WritingTask("t"))
    ctx = ToolContext(session=session, repo=repo, gate=GuardrailGate(quality_gate=_Q()), elicitor=AutoElicitor())
    registry = ToolRegistry()
    register_external_tools(registry, ctx, _FakeWritingProvider())
    out = registry.call("inject_intro")
    assert "未通过护栏" in out
    assert ctx.session.workspace.section_drafts["intro"].content != "外部工具写入的引言。"


def test_external_tool_failure_is_fed_back_not_raised():
    ctx = _ctx()
    registry = ToolRegistry()
    register_external_tools(registry, ctx, _FailingProvider())
    out = registry.call("flaky")
    assert "调用失败" in out and "外部服务不可用" in out
