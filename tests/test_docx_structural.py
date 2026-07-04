"""``export.docx_structural`` 测试：结构签名、语义级结构 diff、Format_Gate 挂接。

python-docx 不可用时整文件跳过。验证：
- 只改 run.text（润色）→ 结构签名不变、diff 判 ok；
- 段落/表格数变化 → diff 判 not-ok 并给出中文原因；
- 标题文本改动 → diff 判 not-ok；
- ``FormatGate.docx_structural_diff_check`` 委托到模块函数、行为一致；
- 读取失败（缺文件）→ 保守判 not-ok。
"""

from __future__ import annotations

import pytest

docx = pytest.importorskip("docx")  # 缺 python-docx 则跳过本文件

from paper_agent.export.docx_structural import (  # noqa: E402
    docx_structural_diff_check,
    structural_signature,
)
from paper_agent.export.format_gate import FormatGate  # noqa: E402


def _make_doc(path, *, intro_text, extra_para=False, extra_table=False,
              heading="Introduction"):
    d = docx.Document()
    d.add_heading(heading, level=1)
    d.add_paragraph(intro_text)
    d.add_paragraph("The accuracy reached 95.6 percent on the benchmark used here.")
    t = d.add_table(rows=2, cols=2)
    t.cell(0, 0).text = "Method"
    t.cell(0, 1).text = "Score"
    if extra_para:
        d.add_paragraph("An additional body paragraph appended to the document tail.")
    if extra_table:
        d.add_table(rows=1, cols=1)
    d.save(path)
    return d


_INTRO = "This is the intro paragraph we would like to polish for readability here."
_INTRO_POLISHED = "This is the introduction paragraph we polish for readability here."


def test_signature_stable_under_text_only_change(tmp_path):
    """仅改正文文字（模拟润色）→ 结构签名不变。"""
    a = str(tmp_path / "a.docx")
    b = str(tmp_path / "b.docx")
    _make_doc(a, intro_text=_INTRO)
    _make_doc(b, intro_text=_INTRO_POLISHED)  # 同结构，仅正文文字不同
    assert structural_signature(docx.Document(a)) == structural_signature(docx.Document(b))


def test_diff_ok_when_polish_only(tmp_path):
    """结构相同、仅文字不同 → diff 判 ok、无原因。"""
    pre = str(tmp_path / "pre.docx")
    post = str(tmp_path / "post.docx")
    _make_doc(pre, intro_text=_INTRO)
    _make_doc(post, intro_text=_INTRO_POLISHED)
    diff = docx_structural_diff_check(pre, post)
    assert diff.ok
    assert diff.reasons == []


def test_diff_not_ok_when_paragraph_count_differs(tmp_path):
    """产物多出一个段落 → diff 判 not-ok 且原因提到段落数。"""
    pre = str(tmp_path / "pre.docx")
    post = str(tmp_path / "post.docx")
    _make_doc(pre, intro_text=_INTRO)
    _make_doc(post, intro_text=_INTRO, extra_para=True)
    diff = docx_structural_diff_check(pre, post)
    assert not diff.ok
    assert any("段落数" in r for r in diff.reasons)


def test_diff_not_ok_when_table_count_differs(tmp_path):
    """产物多出一个表格 → diff 判 not-ok 且原因提到表格数。"""
    pre = str(tmp_path / "pre.docx")
    post = str(tmp_path / "post.docx")
    _make_doc(pre, intro_text=_INTRO)
    _make_doc(post, intro_text=_INTRO, extra_table=True)
    diff = docx_structural_diff_check(pre, post)
    assert not diff.ok
    assert any("表格数" in r for r in diff.reasons)


def test_diff_not_ok_when_heading_text_changes(tmp_path):
    """标题文本被改动 → diff 判 not-ok 且原因提到标题结构。"""
    pre = str(tmp_path / "pre.docx")
    post = str(tmp_path / "post.docx")
    _make_doc(pre, intro_text=_INTRO, heading="Introduction")
    _make_doc(post, intro_text=_INTRO, heading="Background")
    diff = docx_structural_diff_check(pre, post)
    assert not diff.ok
    assert any("标题" in r for r in diff.reasons)


def test_diff_not_ok_when_file_missing(tmp_path):
    """读取失败（产物不存在）→ 保守判 not-ok。"""
    pre = str(tmp_path / "pre.docx")
    _make_doc(pre, intro_text=_INTRO)
    diff = docx_structural_diff_check(pre, str(tmp_path / "does_not_exist.docx"))
    assert not diff.ok
    assert diff.reasons


def test_format_gate_delegates_to_module(tmp_path):
    """FormatGate.docx_structural_diff_check 委托到模块函数、行为一致。"""
    pre = str(tmp_path / "pre.docx")
    post_ok = str(tmp_path / "post_ok.docx")
    post_bad = str(tmp_path / "post_bad.docx")
    _make_doc(pre, intro_text=_INTRO)
    _make_doc(post_ok, intro_text=_INTRO_POLISHED)
    _make_doc(post_bad, intro_text=_INTRO, extra_para=True)

    gate = FormatGate()
    assert gate.docx_structural_diff_check(pre, post_ok).ok
    assert not gate.docx_structural_diff_check(pre, post_bad).ok
