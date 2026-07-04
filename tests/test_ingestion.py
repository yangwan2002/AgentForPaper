"""文档加载层测试：扩展名分发、文本/docx 加载、错误处理。"""

from __future__ import annotations

import pytest

from paper_agent.ingestion import load_document, supported_extensions
from paper_agent.ingestion.loaders import DocumentLoadError


def test_supported_extensions_include_common_formats():
    exts = supported_extensions()
    for e in (".txt", ".md", ".pdf", ".docx"):
        assert e in exts


def test_load_text_and_md(tmp_path):
    p = tmp_path / "draft.md"
    p.write_text("# 引言\n内容[1]。", encoding="utf-8")
    assert "引言" in load_document(str(p))


def test_load_text_strips_bom(tmp_path):
    p = tmp_path / "draft.txt"
    p.write_bytes("\ufeff# 标题".encode("utf-8"))
    text = load_document(str(p))
    assert text.startswith("#")  # BOM 已被去除


def test_unsupported_extension_raises(tmp_path):
    p = tmp_path / "x.rtf"
    p.write_text("hi", encoding="utf-8")
    with pytest.raises(DocumentLoadError):
        load_document(str(p))


def test_missing_file_raises():
    with pytest.raises(DocumentLoadError):
        load_document("no_such_file.md")


def test_load_docx_roundtrip(tmp_path):
    docx = pytest.importorskip("docx")
    path = tmp_path / "draft.docx"
    d = docx.Document()
    d.add_paragraph("引言部分")
    d.add_paragraph("方法部分 [1]")
    d.save(str(path))
    text = load_document(str(path))
    assert "引言部分" in text and "方法部分" in text


def test_load_docx_table_to_markdown_preserves_numbers(tmp_path):
    """docx 表格应转为 Markdown 表格，保留数值（核心诉求）。"""
    docx = pytest.importorskip("docx")
    path = tmp_path / "data.docx"
    d = docx.Document()
    d.add_paragraph("实验结果如下：")
    table = d.add_table(rows=2, cols=2)
    table.cell(0, 0).text = "方法"
    table.cell(0, 1).text = "准确率"
    table.cell(1, 0).text = "Ours"
    table.cell(1, 1).text = "0.92"
    d.save(str(path))
    text = load_document(str(path))
    assert "| 方法 | 准确率 |" in text
    assert "0.92" in text   # 表格数值被精确保留


def test_load_pdf_roundtrip(tmp_path):
    """若环境装了 reportlab 则生成真实 PDF 验证；否则跳过。"""
    pytest.importorskip("pypdf")
    reportlab = pytest.importorskip("reportlab")
    from reportlab.pdfgen import canvas  # noqa: WPS433

    path = tmp_path / "draft.pdf"
    c = canvas.Canvas(str(path))
    c.drawString(100, 750, "Introduction section [1]")
    c.save()
    text = load_document(str(path))
    assert "Introduction" in text
