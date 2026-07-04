"""LaTeX 原地润色测试：结构保护分段、往返无损、守卫、Mock no-op、实际润色。"""

from __future__ import annotations

from hypothesis import given, settings
from hypothesis import strategies as st

from paper_agent.latex_inplace import (
    InplaceLatexPolisher,
    SegmentKind,
    segment_latex,
)
from paper_agent.providers.llm.base import LLMResponse

_SAMPLE = r"""\documentclass{article}
\usepackage{amsmath}
\begin{document}
\section{Introduction}
This is the intro paragraph that we would like to polish for langauge quality.
It cites \cite{smith2020} and refers to \ref{fig:1} and has math $E=mc^2$ inline.
\begin{equation}
a^2 + b^2 = c^2
\end{equation}
Another prose sentence with a number 95.6 and citation [arxiv:1706.03762] here.
% this is a comment that must not change
\end{document}
"""


class _ScriptedLLM:
    def __init__(self, mapping):
        self._mapping = mapping

    def complete(self, messages, **opts):
        user_text = messages[-1].content
        for src, dst in self._mapping.items():
            if src in user_text:
                return LLMResponse(content=dst)
        return LLMResponse(content="")


# --- 分段与往返无损 ---


def test_segment_roundtrip_lossless():
    segs = segment_latex(_SAMPLE)
    assert "".join(s.text for s in segs) == _SAMPLE


@settings(max_examples=100)
@given(st.text())
def test_segment_roundtrip_property(source):
    # Feature: latex-inplace, Property: 分段对任意输入往返无损
    segs = segment_latex(source)
    assert "".join(s.text for s in segs) == source


def test_preamble_and_math_protected():
    segs = segment_latex(_SAMPLE)
    protected_text = "".join(s.text for s in segs if s.kind == SegmentKind.PROTECTED)
    # preamble、公式环境、行内数学、\cite、\ref、注释都应落在保护段。
    assert r"\documentclass{article}" in protected_text
    assert r"a^2 + b^2 = c^2" in protected_text
    assert r"$E=mc^2$" in protected_text
    assert r"\cite{smith2020}" in protected_text
    assert r"\ref{fig:1}" in protected_text
    assert "this is a comment that must not change" in protected_text


# --- Mock no-op ---


def test_mock_is_noop():
    polisher = InplaceLatexPolisher(_ScriptedLLM({}), is_mock=True)
    result = polisher.polish(_SAMPLE)
    assert result.source == _SAMPLE
    assert result.polished_segments == 0


# --- 实际润色：结构保留、只改散文 ---


def test_polish_applies_and_preserves_structure():
    original_core = (
        "This is the intro paragraph that we would like to polish for langauge quality.\n"
        "It cites \\cite{smith2020} and refers to \\ref{fig:1} and has math $E=mc^2$ inline."
    )
    # 该散文段其实包含 \cite/\ref/$...$——它们由带参命令/行内数学规则保护后，
    # 会被切成更小的散文片段。这里针对"纯散文首句"做润色映射即可。
    src = "Plain prose sentence with a typo langauge here to be fixed by editor now."
    polished = "Plain prose sentence with a typo language here to be fixed by the editor now."
    doc = (
        "\\documentclass{article}\n\\begin{document}\n"
        + src
        + "\n\\end{document}\n"
    )
    polisher = InplaceLatexPolisher(_ScriptedLLM({src: polished}), is_mock=False)
    result = polisher.polish(doc)
    assert polished in result.source
    assert result.polished_segments == 1
    # 结构逐字保留。
    assert "\\documentclass{article}" in result.source
    assert "\\begin{document}" in result.source
    assert "\\end{document}" in result.source


def test_guard_rejects_new_latex_command():
    src = "A simple sentence about matching methods for evaluation across views today."
    bad = "A simple \\textbf{sentence} about matching methods for evaluation across views today."
    doc = "\\begin{document}\n" + src + "\n\\end{document}\n"
    polisher = InplaceLatexPolisher(_ScriptedLLM({src: bad}), is_mock=False)
    result = polisher.polish(doc)
    # 引入了新 \textbf → 守卫拦截，原文保留。
    assert bad not in result.source
    assert src in result.source
    assert result.rejected_by_guard == 1


def test_nested_brace_command_fully_protected():
    # \caption{\textbf{Fig 1.} ...} 的嵌套内层必须整体被保护，不送 LLM。
    doc = (
        "\\begin{document}\n"
        "Some intro prose sentence that is long enough to be polished here today.\n"
        "\\caption{\\textbf{Figure 1.} Accuracy over epochs with value 12.5 shown.}\n"
        "\\footnote{See \\cite{smith2020} for details on the 42 baselines.}\n"
        "\\end{document}\n"
    )
    segs = segment_latex(doc)
    protected = "".join(s.text for s in segs if s.kind == SegmentKind.PROTECTED)
    # 整个嵌套 \caption{...} 与 \footnote{...} 都在保护段内。
    assert "\\caption{\\textbf{Figure 1.} Accuracy over epochs with value 12.5 shown.}" in protected
    assert "\\footnote{See \\cite{smith2020} for details on the 42 baselines.}" in protected
    # 往返无损。
    assert "".join(s.text for s in segs) == doc


def test_guard_rejects_number_change():
    src = "The accuracy reached 95.6 percent across all evaluated benchmark datasets here."
    bad = "The accuracy reached 96.5 percent across all evaluated benchmark datasets here."
    doc = "\\begin{document}\n" + src + "\n\\end{document}\n"
    polisher = InplaceLatexPolisher(_ScriptedLLM({src: bad}), is_mock=False)
    result = polisher.polish(doc)
    assert bad not in result.source
    assert src in result.source
    assert result.rejected_by_guard == 1
