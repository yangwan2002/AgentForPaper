"""文档加载层测试：扩展名分发、文本/docx 加载、错误处理。"""

from __future__ import annotations

import pytest

from paper_agent.ingestion import (
    IngestionConfirmationRequired,
    assess_ingestion_quality,
    ingest_document,
    load_document,
    load_document_with_quality,
    split_document_sections,
    supported_extensions,
)
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
    ingested = ingest_document(str(path))
    assert "Introduction" in ingested.text
    assert ingested.quality.metrics["page_count"] == 1


def test_quality_rejects_severely_corrupted_text(tmp_path):
    path = tmp_path / "broken.txt"
    path.write_text("\ufffd" * 120 + " (cid:42)" * 120, encoding="utf-8")

    with pytest.raises(DocumentLoadError, match="摄入质量检查失败"):
        load_document(str(path))


def test_quality_warning_profile_is_consumable(tmp_path):
    path = tmp_path / "edge.md"
    path.write_text("# Introduction\nMostly readable text with one \ufffd marker.", encoding="utf-8")

    ingested = ingest_document(str(path))

    assert ingested.quality.severity == "warning"
    profile = ingested.quality.to_profile()
    assert profile["score"] < 100
    assert profile["metrics"]["replacement_char_count"] == 1
    assert profile["warnings"]


def test_structure_gap_requires_explicit_confirmation(tmp_path):
    path = tmp_path / "unstructured.md"
    path.write_text("Readable academic prose. " * 220, encoding="utf-8")

    with pytest.raises(IngestionConfirmationRequired) as raised:
        load_document(str(path))

    assert raised.value.report.status == "confirmation_required"
    text, report = load_document_with_quality(str(path), confirm=True)
    assert text.startswith("Readable")
    assert report.confirmation_required is True
    assert ingest_document(str(path), allow_confirmation=True).text == text


def test_quality_reuses_acceptance_mojibake_detector(monkeypatch):
    calls = []

    def fake_detector(text):
        calls.append(text)
        return True, "shared detector evidence"

    monkeypatch.setattr(
        "paper_agent.ingestion.quality.detect_mojibake", fake_detector
    )
    report = assess_ingestion_quality("otherwise readable")
    assert calls == ["otherwise readable"]
    assert report.status == "rejected"
    assert "shared detector evidence" in report.fatal_reasons[0]


def test_quality_metrics_cover_cjk_printable_and_structure():
    report = assess_ingestion_quality("# 方法\n这是正文。\n")

    assert report.metrics["cjk_ratio"] > 0
    assert report.metrics["printable_ratio"] == 1.0
    assert report.metrics["section_heading_count"] == 1


def test_latex_subsection_stays_in_parent_section():
    text = (
        "\\chapter{Overview}\nchapter body\n"
        "\\section{Method}\nmethod body\n"
        "\\subsection{Details}\ndetail body\n"
        "\\section{Results}\nresult body\n"
    )

    sections = split_document_sections(text)

    assert [title for _sid, title, _body in sections] == [
        "Overview",
        "Method",
        "Results",
    ]
    method = sections[1][2]
    assert "\\subsection{Details}" in method
    assert "detail body" in method


def test_unified_splitter_uses_academic_fallback():
    text = "1 Introduction\nintro body\n2 Methods\nmethod body\n3 Results\nresults body"
    sections = split_document_sections(text)
    assert [title for _sid, title, _body in sections] == [
        "1 Introduction",
        "2 Methods",
        "3 Results",
    ]
