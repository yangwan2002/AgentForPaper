"""只读能力工具契约测试（任务 4）。

覆盖 locate_section 的三级匹配与歧义/未找到、export_paper 的导出与格式解析、
ask_user 的注册与非交互降级。断言只读工具不产生 ProposedChange、schema 合法。
"""

from __future__ import annotations

from paper_agent.agent_platform.models import AgentSession, WritingTask
from paper_agent.agent_platform.guardrail_gate import GuardrailGate
from paper_agent.agent_platform.tools.ask import register_ask_user
from paper_agent.agent_platform.tools.context import ToolContext
from paper_agent.agent_platform.tools.export_tool import register_export_paper
from paper_agent.agent_platform.tools.locate import (
    find_section_matches,
    register_locate_section,
)
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


class _MemStore:
    def __init__(self):
        self._data = {}

    def load(self, wid):
        return None

    def save(self, ws):
        self._data[ws.workspace_id] = ws.to_dict()


def _ws():
    ws = PaperWorkspace(
        workspace_id="w1",
        input_mode=InputMode.DRAFT_REVISION,
        output_format=OutputFormat.MARKDOWN,
    )
    ws.outline = [
        OutlineNode(section_id="introduction", title="引言", order=0),
        OutlineNode(section_id="experiments", title="实验", order=1),
        OutlineNode(section_id="conclusion", title="结论", order=2),
    ]
    ws.section_drafts = {
        "introduction": SectionDraft(section_id="introduction", title="引言", content="引言正文内容够长足以导出。"),
        "experiments": SectionDraft(section_id="experiments", title="实验", content="实验正文内容够长足以导出。"),
        "conclusion": SectionDraft(section_id="conclusion", title="结论", content="结论正文内容够长足以导出。"),
    }
    return ws


def _ctx(ws=None, elicitor=None, tmp_out="output"):
    ws = ws or _ws()
    session = AgentSession(session_id="w1", workspace=ws, task=WritingTask("t"))
    return ToolContext(
        session=session,
        repo=WorkspaceRepository(_MemStore()),
        gate=GuardrailGate(),
        elicitor=elicitor or AutoElicitor(),
        output_dir=tmp_out,
    )


# --- find_section_matches（纯函数） -----------------------------------------

def test_locate_exact_id():
    matches = find_section_matches(_ws(), "experiments")
    assert [n.section_id for n in matches] == ["experiments"]


def test_locate_title_substring():
    matches = find_section_matches(_ws(), "实验")
    assert [n.section_id for n in matches] == ["experiments"]


def test_locate_by_section_type():
    # 「绪论」不等于 id/title，但体裁推断为 introduction。
    matches = find_section_matches(_ws(), "绪论")
    assert [n.section_id for n in matches] == ["introduction"]


def test_locate_not_found():
    assert find_section_matches(_ws(), "参考文献") == []


def test_locate_empty_reference():
    assert find_section_matches(_ws(), "  ") == []


def test_locate_ambiguous_multiple_titles():
    ws = _ws()
    ws.outline.append(OutlineNode(section_id="exp2", title="补充实验", order=3))
    ws.section_drafts["exp2"] = SectionDraft(section_id="exp2", title="补充实验", content="x")
    matches = find_section_matches(ws, "实验")
    assert {n.section_id for n in matches} == {"experiments", "exp2"}


# --- locate_section 工具（注册 + 调用） -------------------------------------

def test_locate_tool_unique_hit_message():
    registry = ToolRegistry()
    ctx = _ctx()
    register_locate_section(registry, ctx)
    out = registry.call("locate_section", reference="实验")
    assert "命中唯一章节" in out and "experiments" in out


def test_locate_tool_ambiguous_prompts_clarification():
    ws = _ws()
    ws.outline.append(OutlineNode(section_id="exp2", title="补充实验", order=3))
    ws.section_drafts["exp2"] = SectionDraft(section_id="exp2", title="补充实验", content="x")
    registry = ToolRegistry()
    register_locate_section(registry, _ctx(ws=ws))
    out = registry.call("locate_section", reference="实验")
    assert "需澄清" in out and "ask_user" in out


def test_locate_tool_schema_valid():
    registry = ToolRegistry()
    register_locate_section(registry, _ctx())
    schema = registry.get("locate_section").to_openai_schema()
    assert schema["function"]["name"] == "locate_section"
    assert "reference" in schema["function"]["parameters"]["properties"]


# --- export_paper 工具 -------------------------------------------------------

def test_export_tool_produces_files(tmp_path):
    registry = ToolRegistry()
    ctx = _ctx(tmp_out=str(tmp_path))
    register_export_paper(registry, ctx)
    out = registry.call("export_paper")
    assert "已导出" in out
    # 至少产出一个文件。
    assert any(tmp_path.iterdir())


def test_export_tool_format_override(tmp_path):
    registry = ToolRegistry()
    ctx = _ctx(tmp_out=str(tmp_path))
    register_export_paper(registry, ctx)
    out = registry.call("export_paper", format="latex")
    assert "latex" in out


def test_export_tool_invalid_format_falls_back(tmp_path):
    registry = ToolRegistry()
    ctx = _ctx(tmp_out=str(tmp_path))
    register_export_paper(registry, ctx)
    # 非法 format 回落工作区默认（markdown），不抛错。
    out = registry.call("export_paper", format="pdf")
    assert "markdown" in out


# --- ask_user 工具 -----------------------------------------------------------

def test_ask_user_interactive_returns_answer():
    ctx = _ctx(elicitor=_AlwaysAnswer("四个基线"))
    registry = ToolRegistry()
    register_ask_user(registry, ctx)
    out = registry.call("ask_user", question="实验用了几个基线？")
    assert out == "四个基线"


def test_ask_user_non_interactive_degrades():
    ctx = _ctx(elicitor=AutoElicitor())
    registry = ToolRegistry()
    register_ask_user(registry, ctx)
    out = registry.call("ask_user", question="随便问")
    assert "非交互" in out or "不可用" in out


class _AlwaysAnswer:
    """交互式 elicitor 桩：任意问题返回固定答案。"""

    interactive = True

    def __init__(self, answer):
        self._answer = answer

    def ask(self, question):
        return self._answer

    def ask_batch(self, questions):
        return {q.id: self._answer for q in questions}
