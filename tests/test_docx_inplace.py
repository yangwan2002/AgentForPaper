"""DOCX 原地润色测试：只改正文散文、保结构（标题/表格）、守卫、Mock no-op、回滚。

python-docx 不可用时整文件跳过。"""

from __future__ import annotations

import os

import pytest

docx = pytest.importorskip("docx")  # 缺 python-docx 则跳过本文件

from paper_agent.docx_inplace import InplaceDocxPolisher  # noqa: E402
from paper_agent.providers.llm.base import LLMResponse  # noqa: E402


class _ScriptedLLM:
    def __init__(self, mapping):
        self._mapping = mapping

    def complete(self, messages, **opts):
        user_text = messages[-1].content
        for src, dst in self._mapping.items():
            if src in user_text:
                return LLMResponse(content=dst)
        return LLMResponse(content="")


def _make_doc(path):
    d = docx.Document()
    d.add_heading("Introduction Section Title", level=1)
    d.add_paragraph(
        "This is the intro paragraph which we would like to polish for langauge quality "
        "and better readability across the whole document body here."
    )
    d.add_paragraph(
        "The accuracy reached 95.6 percent on the benchmark dataset used in this study here."
    )
    t = d.add_table(rows=2, cols=2)
    t.cell(0, 0).text = "Method"
    t.cell(0, 1).text = "Score"
    d.save(path)
    return d


def _read(path):
    d = docx.Document(path)
    return d, [p.text for p in d.paragraphs], len(d.tables)


def test_mock_is_noop(tmp_path):
    src = str(tmp_path / "in.docx")
    out = str(tmp_path / "out.docx")
    _make_doc(src)
    result = InplaceDocxPolisher(_ScriptedLLM({}), is_mock=True).polish(src, out)
    assert os.path.exists(out)
    assert result.polished_paragraphs == 0
    _, paras_in, _ = _read(src)
    _, paras_out, _ = _read(out)
    assert paras_in == paras_out  # 文字不变


def test_polishes_prose_preserves_heading_and_table(tmp_path):
    src = str(tmp_path / "in.docx")
    out = str(tmp_path / "out.docx")
    _make_doc(src)
    original = (
        "This is the intro paragraph which we would like to polish for langauge quality "
        "and better readability across the whole document body here."
    )
    polished = (
        "This is the introduction paragraph that we would like to polish for language "
        "quality and better readability across the whole document body here."
    )
    llm = _ScriptedLLM({original: polished})
    result = InplaceDocxPolisher(llm, is_mock=False).polish(src, out)

    assert not result.rolled_back
    assert result.polished_paragraphs >= 1
    _, paras, n_tables = _read(out)
    # 标题原样保留（结构型样式跳过）。
    assert "Introduction Section Title" in paras
    # 散文被润色。
    assert polished in paras
    # 表格保留。
    assert n_tables == 1


def test_guard_rejects_number_change(tmp_path):
    src = str(tmp_path / "in.docx")
    out = str(tmp_path / "out.docx")
    _make_doc(src)
    original = (
        "The accuracy reached 95.6 percent on the benchmark dataset used in this study here."
    )
    bad = (
        "The accuracy reached 96.5 percent on the benchmark dataset used in this study here."
    )
    result = InplaceDocxPolisher(_ScriptedLLM({original: bad}), is_mock=False).polish(src, out)
    _, paras, _ = _read(out)
    assert original in paras   # 篡改数字被守卫拦截，原文保留
    assert bad not in paras
    assert result.rejected_by_guard >= 1


def test_structure_signature_stable_under_polish(tmp_path):
    """润色后文档结构签名不变（段落/表格数、标题文本一致）。"""
    from paper_agent.docx_inplace import _structural_signature

    src = str(tmp_path / "in.docx")
    out = str(tmp_path / "out.docx")
    _make_doc(src)
    original = (
        "This is the intro paragraph which we would like to polish for langauge quality "
        "and better readability across the whole document body here."
    )
    polished = (
        "This is the introduction paragraph that we would like to polish for language "
        "quality and better readability across the whole document body here."
    )
    InplaceDocxPolisher(_ScriptedLLM({original: polished}), is_mock=False).polish(src, out)
    sig_in = _structural_signature(docx.Document(src))
    sig_out = _structural_signature(docx.Document(out))
    assert sig_in == sig_out
