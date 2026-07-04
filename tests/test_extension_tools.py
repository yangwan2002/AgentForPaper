"""扩展工具单元测试（升级 Req 6）。

覆盖只读取材工具 read_section / read_reference、章节级精确编辑 edit_section
以及只读质量检查 run_quality_gate / check_citations。

核心断言之一：所有工具均不变更工作区。通过在调用前后对
`ws.to_dict()` 做快照比对来验证「无副作用」契约。
"""

from __future__ import annotations

from paper_agent.tools.quality_tools import QualityCheckTools
from paper_agent.tools.section_edit_tool import (
    SectionEditTool,
    build_section_edit_tool,
)
from paper_agent.tools.workspace_tools import WorkspaceReadTools, WorkspaceView
from paper_agent.workspace.models import (
    InputMode,
    OutlineNode,
    PaperWorkspace,
    ReferenceEntry,
    SectionDraft,
)


def _ws() -> PaperWorkspace:
    """构造一个含一个章节与一条已验证文献的工作区。"""
    ws = PaperWorkspace(
        workspace_id="w", input_mode=InputMode.GENERATION, topic_background="x"
    )
    ws.outline = [OutlineNode(section_id="intro", title="引言", order=0)]
    ws.verified_references = [
        ReferenceEntry(
            id="r1",
            title="可靠的 SLAM 综述",
            authors=["张三", "李四"],
            year=2021,
            source_id="10.1000/xyz",
            source="openalex",
            verified=True,
            abstract="本文综述空地协同 SLAM 的研究进展。",
        )
    ]
    ws.section_drafts = {
        "intro": SectionDraft(
            section_id="intro",
            title="引言",
            content="第一段独特锚点 ALPHA。\n第二段重复词 重复词 收尾。",
            cited_reference_ids=["r1"],
        )
    }
    return ws


# --------------------------------------------------------------------------- #
# read_section / read_reference（Req 6.2 / 6.3 / 6.10 / 6.11）
# --------------------------------------------------------------------------- #


def test_read_section_hit_returns_content_no_mutation():
    ws = _ws()
    before = ws.to_dict()
    tools = WorkspaceReadTools(WorkspaceView(ws))

    result = tools.read_section("intro")

    assert "section_id: intro" in result
    assert "引言" in result
    assert "第一段独特锚点 ALPHA。" in result
    assert "r1" in result
    assert ws.to_dict() == before  # 无副作用


def test_read_section_miss_returns_error_no_mutation():
    ws = _ws()
    before = ws.to_dict()
    tools = WorkspaceReadTools(WorkspaceView(ws))

    result = tools.read_section("nonexistent")

    assert result.startswith("错误：")
    assert "nonexistent" in result
    assert ws.to_dict() == before  # 无副作用


def test_read_reference_hit_returns_metadata_no_mutation():
    ws = _ws()
    before = ws.to_dict()
    tools = WorkspaceReadTools(WorkspaceView(ws))

    result = tools.read_reference("r1")

    assert "id: r1" in result
    assert "可靠的 SLAM 综述" in result
    assert "张三" in result
    assert "2021" in result
    assert "本文综述空地协同 SLAM 的研究进展。" in result
    assert ws.to_dict() == before  # 无副作用


def test_read_reference_miss_returns_error_no_mutation():
    ws = _ws()
    before = ws.to_dict()
    tools = WorkspaceReadTools(WorkspaceView(ws))

    result = tools.read_reference("r999")

    assert result.startswith("错误：")
    assert "r999" in result
    assert ws.to_dict() == before  # 无副作用


def test_workspace_view_returns_copy_not_underlying_object():
    """只读投影返回副本，外部修改不影响底层工作区。"""
    ws = _ws()
    before = ws.to_dict()
    view = WorkspaceView(ws)

    draft = view.get_section("intro")
    assert draft is not None
    draft.content = "外部篡改"
    draft.cited_reference_ids.append("hacked")

    ref = view.get_reference("r1")
    assert ref is not None
    ref.title = "外部篡改"

    assert ws.to_dict() == before  # 底层未被改动


# --------------------------------------------------------------------------- #
# edit_section（Req 6.4 / 6.5 / 6.6 / 6.12）
# --------------------------------------------------------------------------- #


def test_edit_section_unique_anchor_accumulates_one_edit():
    ws = _ws()
    before = ws.to_dict()
    tool = SectionEditTool(ws)

    result = tool.edit_section(
        section_id="intro",
        anchor="第一段独特锚点 ALPHA。",
        replacement="替换后的内容。",
        mode="replace",
    )

    assert "已记录" in result
    assert len(tool.edits) == 1
    edit = tool.edits[0]
    assert edit.section_id == "intro"
    assert edit.anchor == "第一段独特锚点 ALPHA。"
    assert edit.replacement == "替换后的内容。"
    assert edit.mode == "replace"
    assert ws.to_dict() == before  # 工具不直接写工作区


def test_edit_section_anchor_not_found_no_edit():
    ws = _ws()
    before = ws.to_dict()
    tool = SectionEditTool(ws)

    result = tool.edit_section(
        section_id="intro",
        anchor="不存在的锚点",
        replacement="x",
    )

    assert "编辑失败" in result
    assert "未命中" in result
    assert tool.edits == []
    assert ws.to_dict() == before


def test_edit_section_anchor_multiple_hits_no_edit():
    ws = _ws()
    before = ws.to_dict()
    tool = SectionEditTool(ws)

    # "重复词 " 在 intro 内容中出现两次。
    result = tool.edit_section(
        section_id="intro",
        anchor="重复词",
        replacement="x",
    )

    assert "编辑失败" in result
    assert "不唯一" in result or "命中" in result
    assert tool.edits == []
    assert ws.to_dict() == before


def test_edit_section_invalid_mode_no_edit():
    ws = _ws()
    before = ws.to_dict()
    tool = SectionEditTool(ws)

    result = tool.edit_section(
        section_id="intro",
        anchor="第一段独特锚点 ALPHA。",
        replacement="x",
        mode="overwrite",  # 非法 mode
    )

    assert "编辑失败" in result
    assert "mode" in result
    assert tool.edits == []
    assert ws.to_dict() == before


def test_edit_section_nonexistent_section_no_edit():
    ws = _ws()
    before = ws.to_dict()
    tool = SectionEditTool(ws)

    result = tool.edit_section(
        section_id="missing",
        anchor="任意",
        replacement="x",
    )

    assert "编辑失败" in result
    assert "不存在" in result
    assert tool.edits == []
    assert ws.to_dict() == before


def test_edit_section_insert_modes_accumulate():
    ws = _ws()
    tool = SectionEditTool(ws)

    for mode in ("insert_after", "insert_before"):
        tool.edits.clear()
        result = tool.edit_section(
            section_id="intro",
            anchor="第一段独特锚点 ALPHA。",
            replacement="补充。",
            mode=mode,
        )
        assert "已记录" in result
        assert len(tool.edits) == 1
        assert tool.edits[0].mode == mode


def test_build_section_edit_tool_registers_edit_section():
    ws = _ws()
    registry, tool = build_section_edit_tool(ws)

    assert isinstance(tool, SectionEditTool)
    schemas = registry.to_openai_schemas()
    names = {s["function"]["name"] for s in schemas}
    assert "edit_section" in names


# --------------------------------------------------------------------------- #
# run_quality_gate / check_citations（Req 6.7 / 6.8）
# --------------------------------------------------------------------------- #


def test_run_quality_gate_reports_issues_no_mutation():
    ws = _ws()
    # 制造一个空章节，触发高严重度问题。
    ws.outline.append(OutlineNode(section_id="empty", title="空白章", order=1))
    ws.section_drafts["empty"] = SectionDraft(
        section_id="empty", title="空白章", content=""
    )
    before = ws.to_dict()
    tools = QualityCheckTools(ws)

    result = tools.run_quality_gate()

    assert "质量闸发现" in result
    assert "空白章" in result
    assert ws.to_dict() == before  # 只读


def test_run_quality_gate_passes_clean_paper_no_mutation():
    ws = _ws()
    long = "这是充分展开、内容完整的章节正文。" * 10
    ws.section_drafts["intro"] = SectionDraft(
        section_id="intro",
        title="引言",
        content=long,
        cited_reference_ids=["r1"],
    )
    before = ws.to_dict()
    tools = QualityCheckTools(ws)

    result = tools.run_quality_gate()

    assert "通过" in result
    assert ws.to_dict() == before  # 只读


def test_check_citations_reports_unverified_ids_no_mutation():
    ws = _ws()
    # intro 引用一个不在已验证库的 id。
    ws.section_drafts["intro"].cited_reference_ids = ["r1", "ghost"]
    before = ws.to_dict()
    tools = QualityCheckTools(ws)

    result = tools.check_citations()

    assert "ghost" in result
    assert "r1" not in result.replace("ghost", "")  # r1 已验证，不应被报告
    assert ws.to_dict() == before  # 只读


def test_check_citations_passes_when_all_verified_no_mutation():
    ws = _ws()
    before = ws.to_dict()
    tools = QualityCheckTools(ws)

    result = tools.check_citations()

    assert "通过" in result
    assert ws.to_dict() == before  # 只读
