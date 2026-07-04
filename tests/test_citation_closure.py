"""导出期引用闭合单测（Task 2）。

覆盖：``cited_references`` 只返回被引用文献且保持既定顺序；三个导出器的参考文献表
只列被引用者，未被引用的已验证文献不出现。
"""

from __future__ import annotations

import pytest

from paper_agent.export.citation_closure import cited_reference_ids, cited_references
from paper_agent.export.latex import LatexExporter
from paper_agent.export.markdown import MarkdownExporter
from paper_agent.workspace.models import (
    InputMode,
    OutlineNode,
    OutputFormat,
    PaperWorkspace,
    ReferenceEntry,
    SectionDraft,
)


def _ws(output_format=OutputFormat.MARKDOWN) -> PaperWorkspace:
    ws = PaperWorkspace(
        workspace_id="paper", input_mode=InputMode.GENERATION, output_format=output_format
    )
    ws.outline = [OutlineNode(section_id="s1", title="Intro", order=0)]
    ws.verified_references = [
        ReferenceEntry(id="1", title="Cited One", authors=["A"], year=2020, source_id="d1", verified=True),
        ReferenceEntry(id="2", title="Uncited Two", authors=["B"], year=2021, source_id="d2", verified=True),
        ReferenceEntry(id="3", title="Cited Three", authors=["C"], year=2022, source_id="d3", verified=True),
    ]
    ws.section_drafts = {
        "s1": SectionDraft(
            section_id="s1", title="Intro",
            content="body cites [1] and [3] only",
        )
    }
    return ws


def test_cited_reference_ids_scans_text():
    assert cited_reference_ids(_ws()) == {"1", "3"}


def test_cited_references_preserves_order_and_filters():
    refs = cited_references(_ws())
    assert [r.id for r in refs] == ["1", "3"]  # id=2 未被引用，剔除；顺序保持


def test_cited_references_includes_recorded_ids():
    ws = _ws()
    ws.section_drafts["s1"].content = "no bracket citations here"
    ws.section_drafts["s1"].cited_reference_ids = ["2"]
    assert [r.id for r in cited_references(ws)] == ["2"]


def test_markdown_export_lists_only_cited(tmp_path):
    result = MarkdownExporter().export(_ws(), str(tmp_path))
    content = open(result.files[0], encoding="utf-8").read()
    assert "Cited One" in content
    assert "Cited Three" in content
    assert "Uncited Two" not in content


def test_markdown_reference_numbering_stable(tmp_path):
    result = MarkdownExporter().export(_ws(), str(tmp_path))
    content = open(result.files[0], encoding="utf-8").read()
    # 被引用子集重新连续编号：Cited One→1，Cited Three→2。
    assert "1. A (2020). Cited One" in content
    assert "2. C (2022). Cited Three" in content


def test_latex_export_bib_only_cited(tmp_path):
    result = LatexExporter().export(_ws(OutputFormat.LATEX), str(tmp_path))
    bib = next(f for f in result.files if f.endswith(".bib"))
    bib_content = open(bib, encoding="utf-8").read()
    assert "Cited One" in bib_content
    assert "Cited Three" in bib_content
    assert "Uncited Two" not in bib_content


def test_no_references_no_bibliography(tmp_path):
    ws = _ws(OutputFormat.LATEX)
    ws.section_drafts["s1"].content = "no citations at all"
    result = LatexExporter().export(ws, str(tmp_path))
    tex = next(f for f in result.files if f.endswith(".tex"))
    tex_content = open(tex, encoding="utf-8").read()
    assert r"\bibliography{" not in tex_content
