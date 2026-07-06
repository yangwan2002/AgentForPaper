"""InplaceLatexAugmenter 单元测试（inplace-augment-sections · Task 1）。

覆盖：插入章节/参考文献后 preamble/宏/公式逐字保留、原文为产物子串、bib 单份、
无 section 的回退、失败诚实。
"""

from __future__ import annotations

from paper_agent.inplace_augment import (
    AugmentResult,
    InplaceLatexAugmenter,
    SectionSpec,
)

_TEX = r"""\documentclass{article}
\usepackage{amsmath}
\newcommand{\mycmd}[1]{\textbf{#1}}
\begin{document}
\section{Method}
We propose a model. The loss is $L = \sum_i (y_i - \hat{y}_i)^2$.
\begin{equation}
E = mc^2
\end{equation}
\end{document}
"""


def _aug():
    return InplaceLatexAugmenter()


def test_insert_section_preserves_preamble_and_math():
    out, result = _aug().augment(
        _TEX, sections=[SectionSpec(title="引言", body="这是引言正文。")]
    )
    assert result.ok and result.inserted_sections == 1
    # 新章节插到首个 \section{Method} 之前。
    assert out.index("\\section{引言}") < out.index("\\section{Method}")
    # preamble / 宏 / 公式逐字保留。
    assert "\\newcommand{\\mycmd}[1]{\\textbf{#1}}" in out
    assert "$L = \\sum_i (y_i - \\hat{y}_i)^2$" in out
    assert "E = mc^2" in out
    # 原文是产物的子序列（逐字保留）。
    assert _is_subsequence(_TEX, out)


def test_append_references_single_block_before_end():
    out, result = _aug().augment(
        _TEX, references=["Alice. A Study. 2021.", "Bob. B Study. 2020."]
    )
    assert result.ok and result.inserted_references == 2
    assert out.count("\\begin{thebibliography}") == 1
    assert out.index("\\begin{thebibliography}") < out.index("\\end{document}")
    assert "Alice. A Study. 2021." in out


def test_existing_bibliography_not_duplicated():
    tex = _TEX.replace(
        "\\end{document}",
        "\\begin{thebibliography}{9}\n\\bibitem{x} Old.\n\\end{thebibliography}\n\\end{document}",
    )
    out, result = _aug().augment(tex, references=["New ref."])
    assert out.count("\\begin{thebibliography}") == 1  # 未重复插入
    assert any("已含参考文献" in n for n in result.notes)


def test_no_section_falls_back_to_after_begin_document():
    tex = "\\documentclass{article}\n\\begin{document}\nJust prose.\n\\end{document}\n"
    out, result = _aug().augment(tex, sections=[SectionSpec(title="引言", body="正文")])
    assert result.ok
    assert "\\section{引言}" in out
    assert out.index("\\begin{document}") < out.index("\\section{引言}")
    assert _is_subsequence(tex, out)


def test_combined_sections_and_references():
    out, result = _aug().augment(
        _TEX,
        sections=[SectionSpec(title="引言", body="引言正文。")],
        references=["Ref one."],
    )
    assert result.ok
    assert "\\section{引言}" in out and "\\begin{thebibliography}" in out
    assert _is_subsequence(_TEX, out)


def test_returns_result_type():
    out, result = _aug().augment(_TEX)
    assert isinstance(result, AugmentResult) and result.ok


def _is_subsequence(a: str, b: str) -> bool:
    it = iter(b)
    return all(ch in it for ch in a)
