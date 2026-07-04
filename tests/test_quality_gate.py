"""确定性质量闸测试。"""

from __future__ import annotations

from paper_agent.tools.quality_gate import QualityGate
from paper_agent.workspace.models import (
    InputMode,
    OutlineNode,
    PaperWorkspace,
    ReferenceEntry,
    SectionDraft,
)


def _ws() -> PaperWorkspace:
    ws = PaperWorkspace(
        workspace_id="w", input_mode=InputMode.GENERATION, topic_background="x"
    )
    ws.outline = [
        OutlineNode(section_id="intro", title="引言", order=0),
        OutlineNode(section_id="method", title="方法", order=1),
    ]
    return ws


def test_flags_empty_and_placeholder_sections():
    ws = _ws()
    ws.section_drafts = {
        "intro": SectionDraft(section_id="intro", title="引言", content=""),
        "method": SectionDraft(
            section_id="method", title="方法", content="方法部分 TODO 待补充。" * 10
        ),
    }
    report = QualityGate().check(ws)
    types = {i["type"] for i in report.issues}
    assert "empty_section" in types
    assert "placeholder" in types
    assert report.passed is False  # 含高严重度


def test_flags_invalid_citation():
    ws = _ws()
    long = "充分展开的内容。" * 20
    ws.section_drafts = {
        "intro": SectionDraft(
            section_id="intro", title="引言", content=long,
            cited_reference_ids=["arxiv:doesnotexist"],
        ),
        "method": SectionDraft(section_id="method", title="方法", content=long),
    }
    # 无任何已验证文献 → 引用的 id 非法。
    report = QualityGate().check(ws)
    assert any(i["type"] == "invalid_citation" for i in report.issues)
    assert report.passed is False


def test_passes_clean_paper():
    ws = _ws()
    ws.verified_references = [
        ReferenceEntry(id="r1", title="T", authors=["A"], year=2020,
                       source_id="x", verified=True)
    ]
    # Round 5：包含体裁必备元素——引言提"贡献"、方法提"超参"/"定义"。
    intro = (
        "这是充分展开、内容完整的引言。本文的贡献在于提出 X 方法，"
        "解决 Y 问题。" * 5
    )
    method = (
        "这是充分展开的方法部分。定义记号如下：x ∈ R^d。"
        "训练超参：学习率 0.001、batch size 32。" * 5
    )
    ws.section_drafts = {
        "intro": SectionDraft(section_id="intro", title="引言", content=intro,
                              cited_reference_ids=["r1"]),
        "method": SectionDraft(section_id="method", title="方法", content=method,
                               cited_reference_ids=["r1"]),
    }
    report = QualityGate().check(ws)
    assert report.passed is True
    assert report.high_issues == []
