"""改工作区能力工具契约测试（任务 5）。

验证：
- rewrite_section / polish_section / edit_section_anchor 经单一写路径落盘；
- Section_Scope_Task 改动只作用于目标章节，范围外字节不变；
- 护栏拒绝时不落盘并回传原因；
- add_references 只增补可核验文献并产差额说明；
- 工具 schema 合法。
"""

from __future__ import annotations

from paper_agent.agent_platform.guardrail_gate import GuardrailGate
from paper_agent.agent_platform.models import AgentSession, WritingTask
from paper_agent.agent_platform.tools.context import ToolContext
from paper_agent.agent_platform.tools.edit import (
    apply_section_edit,
    register_edit_section_anchor,
    register_polish_section,
    register_rewrite_section,
)
from paper_agent.agent_platform.tools.read import register_read_section
from paper_agent.agent_platform.tools.references import register_add_references
from paper_agent.elicitation import AutoElicitor
from paper_agent.tools.literature_tool import LiteratureSearchTool
from paper_agent.tools.registry import ToolRegistry
from paper_agent.workspace.models import (
    InputMode,
    OutlineNode,
    PaperWorkspace,
    ReferenceEntry,
    SectionDraft,
    SectionEdit,
)
from paper_agent.workspace.repository import WorkspaceRepository


class _MemStore:
    def __init__(self):
        self._data = {}

    def load(self, wid):
        raw = self._data.get(wid)
        return PaperWorkspace.from_dict(raw) if raw else None

    def save(self, ws):
        import copy
        self._data[ws.workspace_id] = copy.deepcopy(ws.to_dict())


def _ws():
    ws = PaperWorkspace(workspace_id="w1", input_mode=InputMode.DRAFT_REVISION)
    ws.outline = [
        OutlineNode(section_id="intro", title="引言", order=0),
        OutlineNode(section_id="method", title="方法", order=1),
    ]
    ws.section_drafts = {
        "intro": SectionDraft(section_id="intro", title="引言", content="引言原文，锚点段落在此。"),
        "method": SectionDraft(section_id="method", title="方法", content="方法原文不该被动。"),
    }
    return ws


def _ctx(ws=None, gate=None):
    ws = ws or _ws()
    repo = WorkspaceRepository(_MemStore())
    repo.create(ws)
    session = AgentSession(session_id="w1", workspace=ws, task=WritingTask("t"))
    return ToolContext(
        session=session,
        repo=repo,
        gate=gate or GuardrailGate(),
        elicitor=AutoElicitor(),
    )


# --- rewrite_section ---------------------------------------------------------

def test_rewrite_section_persists_via_single_write_path():
    ctx = _ctx()
    registry = ToolRegistry()
    register_rewrite_section(registry, ctx)
    out = registry.call("rewrite_section", section_id="intro", new_content="全新的引言叙述。")
    assert "已完成" in out
    assert ctx.workspace.section_drafts["intro"].content == "全新的引言叙述。"
    assert ctx.repo.load("w1").section_drafts["intro"].content == "全新的引言叙述。"


def test_rewrite_section_scope_isolation():
    ctx = _ctx()
    registry = ToolRegistry()
    register_rewrite_section(registry, ctx)
    registry.call("rewrite_section", section_id="intro", new_content="改了引言。")
    # method 章节字节不变（Property 6）。
    assert ctx.workspace.section_drafts["method"].content == "方法原文不该被动。"


def test_rewrite_section_missing_section():
    ctx = _ctx()
    registry = ToolRegistry()
    register_rewrite_section(registry, ctx)
    out = registry.call("rewrite_section", section_id="nope", new_content="x")
    assert "不存在" in out


def test_rewrite_section_empty_content_rejected():
    ctx = _ctx()
    registry = ToolRegistry()
    register_rewrite_section(registry, ctx)
    out = registry.call("rewrite_section", section_id="intro", new_content="   ")
    assert "为空" in out
    assert ctx.workspace.section_drafts["intro"].content == "引言原文，锚点段落在此。"


def test_rewrite_section_rejected_by_guardrail_not_persisted():
    class _Q:
        def check(self, ws):
            class R:
                issues = [{"type": "placeholder", "severity": "high", "section_id": "intro", "message": "含 TODO 占位"}]
            return R()

    ctx = _ctx(gate=GuardrailGate(quality_gate=_Q()))
    registry = ToolRegistry()
    register_rewrite_section(registry, ctx)
    out = registry.call("rewrite_section", section_id="intro", new_content="TODO")
    assert "未通过护栏" in out and "占位" in out
    assert ctx.workspace.section_drafts["intro"].content == "引言原文，锚点段落在此。"


# --- polish_section ----------------------------------------------------------

def test_rewrite_section_refuses_whole_rewrite_of_references():
    # 学术诚信红线：禁止整段改写参考文献章节（防编造文献）。
    ws = _ws()
    ws.outline.append(OutlineNode(section_id="sec_refs", title="参考文献", order=9))
    ws.section_drafts["sec_refs"] = SectionDraft(
        section_id="sec_refs", title="参考文献", content="[1] 真实作者. 真实标题. 2020."
    )
    ctx = _ctx(ws=ws)
    registry = ToolRegistry()
    register_rewrite_section(registry, ctx)
    out = registry.call(
        "rewrite_section", section_id="sec_refs", new_content="[1] 编造的作者. 编造标题. 2099."
    )
    assert "已拒绝" in out and "参考文献" in out
    # 原参考文献未被改动。
    assert ctx.workspace.section_drafts["sec_refs"].content == "[1] 真实作者. 真实标题. 2020."


def test_edit_section_anchor_still_allowed_on_references():
    # 局部锚点编辑（如修特殊字符）在参考文献上仍允许——它锚定已有文本，不能凭空造。
    ws = _ws()
    ws.outline.append(OutlineNode(section_id="sec_refs", title="参考文献", order=9))
    ws.section_drafts["sec_refs"] = SectionDraft(
        section_id="sec_refs", title="参考文献", content="[1] Peña A. Title. 2020."
    )
    ctx = _ctx(ws=ws)
    registry = ToolRegistry()
    register_edit_section_anchor(registry, ctx)
    out = registry.call(
        "edit_section_anchor", section_id="sec_refs", anchor="Peña", replacement="Pena"
    )
    assert "已完成" in out
    assert "Pena" in ctx.workspace.section_drafts["sec_refs"].content


def test_export_paper_applies_saved_typesetting(tmp_path):
    # export_paper 应自动套用 ws.profile['typesetting']（不因工具顺序丢失排版）。
    import pytest as _pytest
    _pytest.importorskip("docx")
    import docx
    from docx.enum.text import WD_ALIGN_PARAGRAPH

    from paper_agent.agent_platform.tools.export_tool import register_export_paper
    from paper_agent.workspace.models import OutputFormat

    ws = PaperWorkspace(workspace_id="wexp", input_mode=InputMode.DRAFT_REVISION,
                        output_format=OutputFormat.DOCX)
    ws.outline = [OutlineNode(section_id="s", title="正文", order=0)]
    ws.section_drafts = {"s": SectionDraft(section_id="s", title="正文", content="足够长的一段正文内容用于导出与排版检查。")}
    ws.profile = {"typesetting": {"alignment": "justify", "line_spacing": 22.0}}
    repo = WorkspaceRepository(_MemStore()); repo.create(ws)
    session = AgentSession(session_id="wexp", workspace=ws, task=WritingTask("t"))
    ctx = ToolContext(session=session, repo=repo, gate=GuardrailGate(),
                      elicitor=AutoElicitor(), output_dir=str(tmp_path))
    registry = ToolRegistry()
    register_export_paper(registry, ctx)
    out = registry.call("export_paper")
    assert "已套用已保存的排版规格" in out
    produced = tmp_path / "wexp.docx"
    document = docx.Document(str(produced))
    assert any(p.alignment == WD_ALIGN_PARAGRAPH.JUSTIFY for p in document.paragraphs)


def test_polish_section_label_and_persist():
    ctx = _ctx()
    registry = ToolRegistry()
    register_polish_section(registry, ctx)
    out = registry.call("polish_section", section_id="method", new_content="润色后的方法叙述。")
    assert "润色章节" in out
    assert ctx.workspace.section_drafts["method"].content == "润色后的方法叙述。"


# --- edit_section_anchor -----------------------------------------------------

def test_apply_section_edit_modes():
    assert apply_section_edit("abc锚def", SectionEdit("s", "锚", "X", "replace")) == "abcXdef"
    assert apply_section_edit("abc锚def", SectionEdit("s", "锚", "X", "insert_after")) == "abc锚Xdef"
    assert apply_section_edit("abc锚def", SectionEdit("s", "锚", "X", "insert_before")) == "abcX锚def"


def test_edit_section_anchor_replaces_and_persists():
    ctx = _ctx()
    registry = ToolRegistry()
    register_edit_section_anchor(registry, ctx)
    out = registry.call(
        "edit_section_anchor", section_id="intro", anchor="锚点段落", replacement="新段落"
    )
    assert "已完成" in out
    assert "新段落" in ctx.workspace.section_drafts["intro"].content
    assert "锚点段落" not in ctx.workspace.section_drafts["intro"].content


def test_edit_section_anchor_missing_anchor_no_change():
    ctx = _ctx()
    registry = ToolRegistry()
    register_edit_section_anchor(registry, ctx)
    out = registry.call(
        "edit_section_anchor", section_id="intro", anchor="不存在的锚", replacement="x"
    )
    assert "未命中" in out
    assert ctx.workspace.section_drafts["intro"].content == "引言原文，锚点段落在此。"


# --- add_references ----------------------------------------------------------

class _FakeRetrieval:
    """返回固定候选；search 用于 LiteratureSearchTool。"""

    def __init__(self, refs):
        self._refs = refs

    def search(self, query, limit=5):
        return list(self._refs[:limit])

    def fetch_metadata(self, source_id):
        # 只有 source_id 以 real- 开头的算真实存在（供核验）。
        if source_id.startswith("real-"):
            return ReferenceEntry(id="x", title="t", authors=["a"], year=2024, source_id=source_id)
        return None


def _refs():
    return [
        ReferenceEntry(id="r1", title="Real A", authors=["A"], year=2024, source_id="real-1"),
        ReferenceEntry(id="r2", title="Fake B", authors=["B"], year=2024, source_id="fake-2"),
    ]


def test_add_references_only_verifiable_land_and_shortfall_noted():
    from paper_agent.tools.citation import CitationVerifier

    retrieval = _FakeRetrieval(_refs())
    verifier = CitationVerifier(retrieval)
    search_tool = LiteratureSearchTool(retrieval, verifier)
    ctx = _ctx(gate=GuardrailGate(citation_verifier=verifier))
    registry = ToolRegistry()
    register_add_references(registry, ctx, search_tool)

    out = registry.call("add_references", query="topic", limit=5)
    # 可核验的 real-1 被重新编号为数字 id "1" 落盘；fake-2 不入库。
    landed = {r.id for r in ctx.workspace.verified_references}
    assert "1" in landed
    titles = {r.title for r in ctx.workspace.verified_references}
    assert "Real A" in titles and "Fake B" not in titles
    # 返回里明确给出可引用编号 [1]。
    assert "已增补" in out and "[1]" in out


def test_add_references_continues_numbering_from_existing():
    from paper_agent.tools.citation import CitationVerifier

    retrieval = _FakeRetrieval(_refs())
    verifier = CitationVerifier(retrieval)
    search_tool = LiteratureSearchTool(retrieval, verifier)
    ctx = _ctx(gate=GuardrailGate(citation_verifier=verifier))
    # 预置已有编号 1..3 的已验证文献 → 新增应从 4 起编号。
    from paper_agent.workspace.models import ReferenceEntry as _R
    for n in ("1", "2", "3"):
        ctx.workspace.verified_references.append(
            _R(id=n, title=f"old{n}", authors=["x"], year=2020, source_id=f"s{n}", verified=True)
        )
    registry = ToolRegistry()
    register_add_references(registry, ctx, search_tool)
    out = registry.call("add_references", query="topic")
    landed = {r.id for r in ctx.workspace.verified_references}
    assert "4" in landed  # 续编到 4
    assert "[4]" in out


def test_verify_existing_references_verified_land_with_draft_numbering():
    from paper_agent.agent_platform.tools.references import (
        register_verify_existing_references,
    )
    from paper_agent.tools.citation import CitationVerifier

    # 原文带参考文献小节；标题能被 openalex 类检索匹配的算真实（这里用假 retrieval）。
    ws = _ws()
    ws.original_draft = (
        "正文引用了 [1] 和 [2]。\n\n"
        "参考文献\n"
        "1. Mur-Artal R. ORB-SLAM2 a versatile and accurate monocular slam. 2017.\n"
        "2. 完全不存在的伪造文献标题 xxxxx. 2099.\n"
    )
    repo = WorkspaceRepository(_MemStore())
    repo.create(ws)
    session = AgentSession(session_id="w1", workspace=ws, task=WritingTask("t"))
    from paper_agent.agent_platform.tools.context import ToolContext
    ctx = ToolContext(session=session, repo=repo, gate=GuardrailGate(), elicitor=AutoElicitor())

    class _Retrieval:
        def search(self, query, limit=5):
            # 只有含 ORB-SLAM2 的标题能检索到真实记录。
            if "orb-slam2" in query.lower():
                return [ReferenceEntry(id="x", title="ORB-SLAM2 a versatile and accurate monocular slam",
                                       authors=["Mur-Artal R"], year=2017, source_id="10.1/orb")]
            return []

        def fetch_metadata(self, source_id):
            return None

    verifier = CitationVerifier(_Retrieval(), title_threshold=0.5)
    registry = ToolRegistry()
    register_verify_existing_references(registry, ctx, verifier)
    out = registry.call("verify_existing_references")

    ids = {r.id for r in ctx.workspace.verified_references}
    # 第1条（ORB-SLAM2）核验入库、编号保留为 "1"；第2条伪造 → 未入库。
    assert "1" in ids
    assert "2" not in ids
    assert "核验入库" in out


def test_verify_existing_references_no_draft():
    from paper_agent.agent_platform.tools.references import (
        register_verify_existing_references,
    )
    from paper_agent.tools.citation import CitationVerifier

    ctx = _ctx()  # 无 original_draft
    registry = ToolRegistry()

    class _R:
        def search(self, q, limit=5): return []
        def fetch_metadata(self, s): return None

    register_verify_existing_references(registry, ctx, CitationVerifier(_R()))
    out = registry.call("verify_existing_references")
    assert "没有原文内容" in out or "未在原文" in out


def test_add_references_no_results_message():
    from paper_agent.tools.citation import CitationVerifier

    retrieval = _FakeRetrieval([])
    verifier = CitationVerifier(retrieval)
    search_tool = LiteratureSearchTool(retrieval, verifier)
    ctx = _ctx(gate=GuardrailGate(citation_verifier=verifier))
    registry = ToolRegistry()
    register_add_references(registry, ctx, search_tool)
    out = registry.call("add_references", query="topic")
    assert "未检索到" in out


# --- read_section ------------------------------------------------------------

def test_read_section_returns_content():
    ctx = _ctx()
    registry = ToolRegistry()
    register_read_section(registry, ctx)
    out = registry.call("read_section", section_id="method")
    assert "方法原文不该被动。" in out


def test_write_tool_schemas_valid():
    ctx = _ctx()
    registry = ToolRegistry()
    register_rewrite_section(registry, ctx)
    register_edit_section_anchor(registry, ctx)
    for name in ("rewrite_section", "edit_section_anchor"):
        schema = registry.get(name).to_openai_schema()
        assert schema["function"]["name"] == name
        assert schema["function"]["parameters"]["type"] == "object"
