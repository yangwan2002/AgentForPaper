"""导出器测试：LaTeX + BibTeX（Req 10.4/10.5/10.6）。"""

from __future__ import annotations

import os

from paper_agent.export.latex import LatexExporter
from paper_agent.export.factory import get_exporter
from paper_agent.workspace.models import (
    InputMode,
    OutlineNode,
    OutputFormat,
    PaperWorkspace,
    ReferenceEntry,
    SectionDraft,
)


def _ws() -> PaperWorkspace:
    ws = PaperWorkspace(
        workspace_id="paper1",
        input_mode=InputMode.GENERATION,
        output_format=OutputFormat.LATEX,
        topic_background="x",
    )
    ws.outline = [OutlineNode(section_id="intro", title="Introduction", order=0)]
    ws.verified_references = [
        ReferenceEntry(
            id="arxiv:1706.03762", title="Attention Is All You Need",
            authors=["Ashish Vaswani"], year=2017,
            source_id="1706.03762", source="arxiv", verified=True,
        )
    ]
    ws.section_drafts = {
        "intro": SectionDraft(
            section_id="intro", title="Introduction",
            content="Some text with 50% special & chars",
            cited_reference_ids=["arxiv:1706.03762"],
        )
    }
    return ws


def test_latex_export_writes_tex_and_bib(tmp_path):
    result = LatexExporter().export(_ws(), str(tmp_path))
    assert result.output_format is OutputFormat.LATEX
    assert len(result.files) == 2

    tex = next(f for f in result.files if f.endswith(".tex"))
    bib = next(f for f in result.files if f.endswith(".bib"))
    assert os.path.exists(tex) and os.path.exists(bib)

    tex_content = open(tex, encoding="utf-8").read()
    bib_content = open(bib, encoding="utf-8").read()

    assert r"\section{Introduction}" in tex_content
    assert r"\cite{" in tex_content
    assert r"50\%" in tex_content  # 特殊字符被转义
    assert "Vaswani2017" in bib_content
    assert "Attention Is All You Need" in bib_content


def test_factory_returns_registered_exporters():
    assert get_exporter(OutputFormat.LATEX).format is OutputFormat.LATEX
    assert get_exporter(OutputFormat.MARKDOWN).format is OutputFormat.MARKDOWN
    assert get_exporter(OutputFormat.DOCX).format is OutputFormat.DOCX
