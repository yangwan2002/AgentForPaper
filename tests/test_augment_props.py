"""inplace-augment-sections 属性测试（Task 5）。

覆盖：tex 原文为产物子序列（Property 5，只增不改）、章节标题恰新增一次（Property 3）、
docx 结构计数只增不减 + 原稿字节不变（Property 1/2）、失败保留原稿（Property 6）。
"""

from __future__ import annotations

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from paper_agent.inplace_augment import (
    InplaceDocxAugmenter,
    InplaceLatexAugmenter,
    SectionSpec,
)

_ALLOW_TMP = [HealthCheck.function_scoped_fixture]

_TEX = (
    "\\documentclass{article}\n\\usepackage{amsmath}\n\\begin{document}\n"
    "\\section{Method}\n$E=mc^2$\n\\end{document}\n"
)

# 安全正文/标题字母表：不含会干扰 \section 计数或结构的字符。
_SAFE = "abcdefghijklmnopqrstuvwxyz 中文正abc0123.,"


def _is_subsequence(a: str, b: str) -> bool:
    it = iter(b)
    return all(ch in it for ch in a)


# Property 5: tex 原文逐字保留（原文是产物的子序列）。
@settings(max_examples=60)
@given(body=st.text(alphabet=_SAFE, max_size=60), title=st.text(alphabet=_SAFE, min_size=1, max_size=20))
def test_prop5_latex_original_is_subsequence(body, title):
    out, result = InplaceLatexAugmenter().augment(
        _TEX, sections=[SectionSpec(title=title, body=body)]
    )
    assert result.ok
    assert _is_subsequence(_TEX, out)


# Property 3: 章节标题恰新增一次。
@settings(max_examples=50)
@given(title=st.text(alphabet="abcdefghij中文", min_size=1, max_size=12))
def test_prop3_latex_section_added_once(title):
    before = _TEX.count(f"\\section{{{title}}}")
    out, result = InplaceLatexAugmenter().augment(
        _TEX, sections=[SectionSpec(title=title, body="body")]
    )
    after = out.count(f"\\section{{{title}}}")
    assert after == before + 1


# Property 6: latex 增补异常/失败时返回原文（不毁原稿）。
def test_prop6_latex_failure_returns_original(monkeypatch):
    aug = InplaceLatexAugmenter()

    def _boom(*a, **k):
        raise RuntimeError("boom")

    monkeypatch.setattr(aug, "_insert_section", _boom)
    out, result = aug.augment(_TEX, sections=[SectionSpec(title="x")])
    assert result.ok is False
    assert out == _TEX  # 原文原样返回


# Property 1/2: docx 结构计数只增不减 + 原稿字节不变。
@settings(max_examples=15, deadline=None, suppress_health_check=_ALLOW_TMP)
@given(body=st.text(alphabet=_SAFE, max_size=50))
def test_prop12_docx_counts_grow_and_source_unchanged(body, tmp_path):
    docx = pytest.importorskip("docx")
    from paper_agent.export.docx_structural import structural_fields

    src = tmp_path / "p.docx"
    d = docx.Document()
    d.add_heading("方法", level=1)
    d.add_paragraph("原有正文段落。")
    d.add_table(rows=2, cols=2)
    d.save(str(src))
    pre = structural_fields(docx.Document(str(src)))
    original = src.read_bytes()
    out = tmp_path / "p_aug.docx"

    result = InplaceDocxAugmenter().augment(
        str(src), str(out),
        sections=[SectionSpec(title="引言", body=body or "x")],
        references=["Ref one."],
    )
    assert result.ok
    assert src.read_bytes() == original  # 原稿只读
    post = structural_fields(docx.Document(str(out)))
    for key in ("paragraphs", "tables", "drawings", "footnote_refs"):
        assert int(post[key]) >= int(pre[key])  # 只增不减
    # 原有标题仍在。
    assert set(pre["headings"]).issubset(set(post["headings"]))
