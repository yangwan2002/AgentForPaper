"""无 pandoc 回退渲染器的段落切分测试（改进：不再把整章塞进单段落）。"""

from __future__ import annotations

import pytest

docx = pytest.importorskip("docx")

from paper_agent.export.docx import DocxExporter
from paper_agent.export.pandoc_pipeline import PandocConverter
from paper_agent.workspace.models import (
    InputMode,
    OutlineNode,
    OutputFormat,
    PaperWorkspace,
    SectionDraft,
)


class _NoPandoc(PandocConverter):
    """强制 probe 为 False，走回退渲染器。"""

    def probe(self, timeout: float = 5.0) -> bool:
        return False


def _ws_with(content: str) -> PaperWorkspace:
    ws = PaperWorkspace(workspace_id="wfb", input_mode=InputMode.DRAFT_REVISION,
                        output_format=OutputFormat.DOCX)
    ws.outline = [OutlineNode(section_id="s", title="方法", order=0)]
    ws.section_drafts = {"s": SectionDraft(section_id="s", title="方法", content=content)}
    return ws


def _body_paragraph_texts(path: str) -> list[str]:
    d = docx.Document(path)
    # 跳过标题段落（heading 样式）。
    out = []
    for p in d.paragraphs:
        name = (p.style.name or "").lower() if p.style else ""
        if "heading" in name or "title" in name:
            continue
        if p.text.strip():
            out.append(p.text.strip())
    return out


def test_fallback_splits_blank_line_separated_paragraphs(tmp_path):
    content = "第一段的内容。\n\n第二段的内容。\n\n第三段的内容。"
    result = DocxExporter(pandoc=_NoPandoc()).export(_ws_with(content), str(tmp_path))
    paras = _body_paragraph_texts(result.files[0])
    assert "第一段的内容。" in paras
    assert "第二段的内容。" in paras
    assert "第三段的内容。" in paras
    # 三个独立段落，而非一个大段落。
    assert sum(1 for p in paras if p.startswith("第")) == 3


def test_fallback_merges_cjk_hard_wraps(tmp_path):
    # PDF 常在中文句中硬换行——应合并为一个连贯段落，中文字符间不留空格。
    content = "本文提出了一种面\n向空地协同的图\n像匹配方法。"
    result = DocxExporter(pandoc=_NoPandoc()).export(_ws_with(content), str(tmp_path))
    paras = _body_paragraph_texts(result.files[0])
    assert "本文提出了一种面向空地协同的图像匹配方法。" in paras


def test_fallback_renders_list_items(tmp_path):
    content = "主要贡献：\n\n- 第一点贡献\n- 第二点贡献\n- 第三点贡献"
    result = DocxExporter(pandoc=_NoPandoc()).export(_ws_with(content), str(tmp_path))
    paras = _body_paragraph_texts(result.files[0])
    assert "第一点贡献" in paras
    assert "第二点贡献" in paras
    assert "第三点贡献" in paras
