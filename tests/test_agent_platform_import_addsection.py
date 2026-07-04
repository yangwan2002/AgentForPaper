"""import_draft 与 add_section 工具测试（对话模式的文件导入与补写章节）。"""

from __future__ import annotations

import copy

from paper_agent.agent_platform.guardrail_gate import GuardrailGate
from paper_agent.agent_platform.models import AgentSession, WritingTask
from paper_agent.agent_platform.tools.context import ToolContext
from paper_agent.agent_platform.tools.edit import register_add_section
from paper_agent.agent_platform.tools.import_draft import register_import_draft
from paper_agent.elicitation import AutoElicitor
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


def _ctx(gate=None):
    ws = PaperWorkspace(workspace_id="w1", input_mode=InputMode.GENERATION)
    repo = WorkspaceRepository(_MemStore())
    repo.create(ws)
    session = AgentSession(session_id="w1", workspace=ws, task=WritingTask("t"))
    return ToolContext(session=session, repo=repo, gate=gate or GuardrailGate(), elicitor=AutoElicitor())


# --- import_draft ------------------------------------------------------------

def test_import_draft_markdown_splits_sections(tmp_path):
    md = tmp_path / "paper.md"
    md.write_text("# 方法\n方法内容。\n\n# 实验\n实验内容。\n", encoding="utf-8")
    ctx = _ctx()
    registry = ToolRegistry()
    register_import_draft(registry, ctx)
    out = registry.call("import_draft", path=str(md))
    assert "已导入" in out and "2 个章节" in out
    ws = ctx.repo.load("w1")
    assert ws.input_mode is InputMode.DRAFT_REVISION
    titles = {n.title for n in ws.outline}
    assert titles == {"方法", "实验"}
    assert "方法内容。" in ws.section_drafts[ws.outline[0].section_id].content


def test_split_academic_sections_roman_letter_numbered():
    from paper_agent.agent_platform.tools.import_draft import split_academic_sections

    text = (
        "I. 方法\n"
        "方法总述。\n"
        "A. 问题定义\n"
        "定义内容。\n"
        "1 引言部分\n"
        "引言内容。\n"
        "II. 实验与结果\n"
        "实验总述。\n"
    )
    triples = split_academic_sections(text)
    titles = [t for _sid, t, _c in triples]
    assert "I. 方法" in titles
    assert "A. 问题定义" in titles
    assert "II. 实验与结果" in titles


def test_split_academic_rejects_table_number_rows():
    from paper_agent.agent_platform.tools.import_draft import split_academic_sections

    # 表格数字行不应被当作标题（防误报的核心用例）。
    text = (
        "II. 实验\n"
        "结果如下：\n"
        "0.856 0.894 0.922\n"
        "18.6% 25.4%\n"
        "3.3 4.1 5.2\n"
        "A. 消融\n"
        "消融内容。\n"
    )
    triples = split_academic_sections(text)
    titles = [t for _sid, t, _c in triples]
    # 只识别出两个真标题，数字行归入正文。
    assert titles == ["II. 实验", "A. 消融"]
    body = triples[0][2]
    assert "0.856 0.894 0.922" in body  # 数字行保留在实验章节正文里


def test_split_academic_rejects_sentence_starting_with_letter():
    from paper_agent.agent_platform.tools.import_draft import split_academic_sections

    # 句首字母无句点，不应被当作字母编号标题。
    text = "A. 方法\n正文。\nA novel approach without period\n更多正文。\n"
    triples = split_academic_sections(text)
    titles = [t for _sid, t, _c in triples]
    assert titles == ["A. 方法"]


def test_import_draft_missing_file():
    ctx = _ctx()
    registry = ToolRegistry()
    register_import_draft(registry, ctx)
    out = registry.call("import_draft", path="D:/nope/not_here.pdf")
    assert "读取失败" in out


def test_import_draft_strips_quotes(tmp_path):
    md = tmp_path / "p.md"
    md.write_text("# A\nx\n", encoding="utf-8")
    ctx = _ctx()
    registry = ToolRegistry()
    register_import_draft(registry, ctx)
    # 用户常把带引号的路径贴进来。
    out = registry.call("import_draft", path=f'"{md}"')
    assert "已导入" in out


# --- add_section -------------------------------------------------------------

def test_add_section_creates_new_section_at_start():
    ctx = _ctx()
    # 预置一个已有章节。
    from paper_agent.workspace.models import OutlineNode, SectionDraft
    ws = ctx.workspace
    ws.outline = [OutlineNode(section_id="method", title="方法", order=0)]
    ws.section_drafts = {"method": SectionDraft(section_id="method", title="方法", content="方法正文")}
    ctx.repo.update(ws, lambda w: None)

    registry = ToolRegistry()
    register_add_section(registry, ctx)
    out = registry.call("add_section", title="引言", content="本文研究……（足够长的引言正文）。", position="start")
    assert "已完成" in out
    reloaded = ctx.repo.load("w1")
    # 引言以体裁名 introduction 作为 id，且排在方法之前。
    assert "introduction" in reloaded.section_drafts
    ordered = reloaded.ordered_sections()
    assert ordered[0].section_id == "introduction"


def test_add_section_empty_content_rejected():
    ctx = _ctx()
    registry = ToolRegistry()
    register_add_section(registry, ctx)
    out = registry.call("add_section", title="引言", content="   ")
    assert "为空" in out


def test_add_section_rejected_by_guardrail():
    class _Q:
        def check(self, ws):
            class R:
                issues = [{"type": "placeholder", "severity": "high", "section_id": "introduction", "message": "含 TODO 占位"}]
            return R()

    ctx = _ctx(gate=GuardrailGate(quality_gate=_Q()))
    registry = ToolRegistry()
    register_add_section(registry, ctx)
    out = registry.call("add_section", title="引言", content="TODO 待补充")
    assert "未通过护栏" in out
    # 未落盘。
    assert "introduction" not in ctx.repo.load("w1").section_drafts
