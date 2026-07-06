"""InplaceDocxAugmenter 单元测试（inplace-augment-sections · Task 2）。

覆盖：插入章节后原段落/表格保留、标题新增一次、参考文献单份+悬挂缩进、
Preservation_Check 失败保留原稿、原稿字节不变。python-docx 缺失跳过。
"""

from __future__ import annotations

import pytest

from paper_agent.inplace_augment import (
    AugmentResult,
    InplaceDocxAugmenter,
    SectionSpec,
)


def _make_docx(path):
    docx = pytest.importorskip("docx")
    document = docx.Document()
    document.add_heading("方法", level=1)
    document.add_paragraph("这是方法部分的正文，含重要内容。")
    table = document.add_table(rows=2, cols=2)
    table.cell(0, 0).text = "指标"
    table.cell(0, 1).text = "数值"
    document.add_paragraph("方法部分的结尾段落。")
    document.save(str(path))
    return document


def test_insert_section_preserves_content_and_adds_heading_once(tmp_path):
    docx = pytest.importorskip("docx")
    src = tmp_path / "paper.docx"
    _make_docx(src)
    original = src.read_bytes()
    out = tmp_path / "paper_aug.docx"

    result = InplaceDocxAugmenter().augment(
        str(src), str(out),
        sections=[SectionSpec(title="引言", body="这是引言正文。\n\n第二段引言。")],
    )
    assert isinstance(result, AugmentResult) and result.ok
    assert result.inserted_sections == 1
    assert src.read_bytes() == original  # 原稿只读

    doc = docx.Document(str(out))
    texts = [p.text for p in doc.paragraphs]
    # 原有内容保留。
    assert "这是方法部分的正文，含重要内容。" in texts
    assert "方法部分的结尾段落。" in texts
    # 原表格保留。
    assert len(doc.tables) == 1
    assert doc.tables[0].cell(0, 0).text == "指标"
    # 新章节标题恰新增一次。
    assert texts.count("引言") == 1
    # 引言插到方法之前。
    assert texts.index("引言") < texts.index("方法")


def test_append_references_single_and_hanging_indent(tmp_path):
    docx = pytest.importorskip("docx")
    src = tmp_path / "paper.docx"
    _make_docx(src)
    out = tmp_path / "paper_aug.docx"

    result = InplaceDocxAugmenter().augment(
        str(src), str(out),
        references=["Alice. A Study. 2021.", "Bob. B Study. 2020."],
    )
    assert result.ok and result.inserted_references == 2
    doc = docx.Document(str(out))
    texts = [p.text for p in doc.paragraphs]
    # 参考文献标题一份。
    assert texts.count("参考文献") == 1
    ref_paras = [p for p in doc.paragraphs if p.text.strip().startswith("1. ")]
    assert ref_paras
    fmt = ref_paras[0].paragraph_format
    assert fmt.first_line_indent is not None and fmt.first_line_indent.pt < 0  # 悬挂


def test_existing_reference_heading_not_duplicated(tmp_path):
    docx = pytest.importorskip("docx")
    src = tmp_path / "paper.docx"
    document = _make_docx(src)
    document.add_heading("参考文献", level=1)  # 已有参考文献标题
    document.save(str(src))
    out = tmp_path / "paper_aug.docx"

    result = InplaceDocxAugmenter().augment(
        str(src), str(out), references=["New ref."]
    )
    assert result.ok
    doc = docx.Document(str(out))
    texts = [p.text for p in doc.paragraphs]
    assert texts.count("参考文献") == 1  # 未重复插标题


def test_preservation_failure_keeps_original(tmp_path):
    docx = pytest.importorskip("docx")

    class _DestructiveAugmenter(InplaceDocxAugmenter):
        def _insert_section(self, document, spec):
            # 破坏原有内容：删掉第一个段落（模拟 re-emit 破坏）。
            p = document.paragraphs[0]._p
            p.getparent().remove(p)

    src = tmp_path / "paper.docx"
    _make_docx(src)
    original = src.read_bytes()
    out = tmp_path / "paper_aug.docx"

    result = _DestructiveAugmenter().augment(
        str(src), str(out), sections=[SectionSpec(title="引言", body="x")]
    )
    # 破坏被 Preservation_Check 逮住 → ok=False、产物回退为原稿、原稿字节不变。
    assert result.ok is False
    assert "结构" in result.error or result.notes
    assert src.read_bytes() == original
    assert out.read_bytes() == original  # 产物是原稿副本，未交付破坏性文件
