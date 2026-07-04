"""LaTeX 原地润色（in-place source polish）。

与既有「内容驱动、重渲染」的管线不同，本模块把用户的 **LaTeX 源当作真相**：
只润色其中的自然语言散文，**逐字节保留** preamble、宏、数学公式、各类环境、
`\\cite/\\ref/\\label`、图表、注释与整体结构。

实现思路（保守优先，宁可少改也不破坏结构）：
1. ``segment_latex(source)`` 把源切成一个**不重叠、全覆盖**的段序列，每段标记为
   ``PROSE``（可润色的散文）或 ``PROTECTED``（结构，逐字保留）。
   保护范围包括：preamble（直到 ``\\begin{document}``）、``\\end{document}`` 之后、
   注释、行内/行间数学、公式/表格/图/代码等环境、以及一批带参命令
   （``\\cite`` / ``\\ref`` / ``\\includegraphics`` / 章节命令等）。
   不确定的一律归入 PROTECTED。
2. 对每个 PROSE 段调用 LLM 润色，并经**确定性守卫**校验：反斜杠命令多重集合、
   花括号/方括号/美元符号计数、数字多重集合、``[id]`` 引用集合必须完全一致，且
   长度浮动在允许区间内——任一不满足即**丢弃润色、保留原文**。
3. 按序拼回，得到与原文结构完全一致、仅散文被润色的新 LaTeX 源。

不变量：``"".join(seg.text for seg in segment_latex(s)) == s``（往返无损）。
Mock provider 下整体 no-op（输出逐字节等于输入）。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from paper_agent.inplace_core import ProsePolishGuard, polish_fragment
from paper_agent.prompts import templates
from paper_agent.providers.llm.base import LLMProvider
from paper_agent.tools import polish_guards

# LaTeX 散文守卫：内容（引用/数字）+ LaTeX 结构（命令/括号）+ 长度均须保持。
_LATEX_GUARD = ProsePolishGuard(
    [
        polish_guards.content_preserved,
        polish_guards.latex_structure_preserved,
        polish_guards.length_ratio_ok,
    ]
)

# 需整体保护的环境名（其内容为结构/数学/代码，绝非散文）。
_PROTECTED_ENVS = [
    "equation", "equation*", "align", "align*", "gather", "gather*",
    "multline", "multline*", "eqnarray", "eqnarray*", "displaymath", "math",
    "array", "matrix", "pmatrix", "bmatrix", "vmatrix", "cases",
    "verbatim", "lstlisting", "minted", "tikzpicture", "pgfpicture",
    "tabular", "tabular*", "tabularx", "table", "table*", "longtable",
    "figure", "figure*", "subfigure", "wrapfigure",
    "algorithm", "algorithmic", "algorithm2e", "listing",
    "thebibliography",
]

# 需连同参数一起保护的命令（不润色其参数，避免破坏引用/标签/路径/标题）。
_PROTECTED_CMDS = [
    "cite", "citep", "citet", "citeauthor", "citeyear", "nocite",
    "ref", "eqref", "autoref", "cref", "Cref", "pageref", "label",
    "includegraphics", "input", "include", "bibliography",
    "bibliographystyle", "usepackage", "documentclass",
    "url", "href", "newcommand", "renewcommand", "def", "DeclareMathOperator",
    "section", "subsection", "subsubsection", "chapter", "paragraph",
    "subparagraph", "part", "title", "author", "date", "institute",
    "caption", "footnote",
]


def _build_protected_patterns() -> list[re.Pattern]:
    envs = "|".join(re.escape(e) for e in _PROTECTED_ENVS)
    return [
        # preamble：文首直到（含）\begin{document}。
        re.compile(r"\A.*?\\begin\{document\}", re.DOTALL),
        # postamble：\end{document} 及其后所有内容。
        re.compile(r"\\end\{document\}.*\Z", re.DOTALL),
        # 注释：未转义的 % 到行尾。
        re.compile(r"(?<!\\)%[^\n]*"),
        # 保护环境（整体）。
        re.compile(
            r"\\begin\{(?P<env>" + envs + r")\}.*?\\end\{(?P=env)\}",
            re.DOTALL,
        ),
        # 行间数学。
        re.compile(r"\\\[.*?\\\]", re.DOTALL),
        re.compile(r"(?<!\\)\$\$.*?\$\$", re.DOTALL),
        re.compile(r"\\\(.*?\\\)", re.DOTALL),
        # 行内数学 $...$（允许 \$ 转义；否定字符类含换行，故天然跨行）。
        re.compile(r"(?<!\\)\$(?:\\.|[^$\\])*\$"),
    ]


_PROTECTED_PATTERNS = _build_protected_patterns()

# 带参命令的命令头（命令名 + 可选 *）——参数的花括号由平衡扫描器处理，
# 以正确保护 ``\caption{\textbf{...}}`` / ``\footnote{...\cite{x}}`` /
# ``\newcommand{}[]{}`` 这类**嵌套**花括号（旧的非嵌套正则会把内层送 LLM）。
_PROTECTED_CMD_HEAD = re.compile(
    r"\\(?:" + "|".join(re.escape(c) for c in _PROTECTED_CMDS) + r")\*?"
)


def _skip_ws(source: str, i: int) -> int:
    n = len(source)
    while i < n and source[i] in " \t\r\n":
        i += 1
    return i


def _consume_balanced_braces(source: str, i: int) -> int:
    """从 ``source[i] == '{'`` 起消费一个花括号平衡组，返回其后一位下标。

    正确处理嵌套 ``{...{...}...}`` 与转义 ``\\{`` / ``\\}``。``source[i]`` 非 ``{``
    时原样返回 ``i``（不消费）。
    """
    n = len(source)
    if i >= n or source[i] != "{":
        return i
    depth = 0
    while i < n:
        ch = source[i]
        if ch == "\\":  # 跳过转义字符（\{ \} \\ 等）
            i += 2
            continue
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return i + 1
        i += 1
    return n  # 未闭合：保护到末尾（保守）


def _consume_optional_bracket(source: str, i: int) -> int:
    """从 ``source[i] == '['`` 起消费一个可选参数 ``[...]``（不嵌套），返回其后下标。"""
    n = len(source)
    if i >= n or source[i] != "[":
        return i
    j = source.find("]", i + 1)
    return (j + 1) if j != -1 else n


def _command_protected_spans(source: str) -> list[tuple[int, int]]:
    """扫描带参命令，返回连同其（可能嵌套的）参数在内的保护区间。

    对每个命令头，依次消费可选 ``[...]`` 与若干平衡 ``{...}`` 组（其间允许空白）。
    """
    spans: list[tuple[int, int]] = []
    for m in _PROTECTED_CMD_HEAD.finditer(source):
        start = m.start()
        i = m.end()
        # 可选参数 [..]（如 \includegraphics[width=..]{..}、\newcommand{}[n]{}）。
        j = _skip_ws(source, i)
        if j < len(source) and source[j] == "[":
            i = _consume_optional_bracket(source, j)
        # 若干必选/可选花括号组（平衡、可嵌套）。
        while True:
            j = _skip_ws(source, i)
            if j < len(source) and source[j] == "{":
                i = _consume_balanced_braces(source, j)
            else:
                break
        spans.append((start, i))
    return spans

# 一个散文段至少包含这么多「字母/汉字」才值得送 LLM 润色（否则纯空白/符号跳过）。
_MIN_PROSE_LETTERS = 15

_LETTER = re.compile(r"[A-Za-z\u4e00-\u9fff]")


class SegmentKind:
    PROSE = "prose"
    PROTECTED = "protected"


@dataclass
class Segment:
    kind: str
    text: str


@dataclass
class InplacePolishResult:
    """原地润色结果。"""

    source: str                       # 润色后的完整 LaTeX 源
    total_prose_segments: int = 0     # 值得润色的散文段总数
    polished_segments: int = 0        # 实际被润色替换的段数
    rejected_by_guard: int = 0        # 因守卫拦截而保留原文的段数
    notes: list[str] = field(default_factory=list)


def _merge_intervals(intervals: list[tuple[int, int]]) -> list[tuple[int, int]]:
    """合并重叠/相接的区间（按起点排序后线性合并）。"""
    if not intervals:
        return []
    intervals = sorted(intervals)
    merged = [intervals[0]]
    for start, end in intervals[1:]:
        last_start, last_end = merged[-1]
        if start <= last_end:
            merged[-1] = (last_start, max(last_end, end))
        else:
            merged.append((start, end))
    return merged


def segment_latex(source: str) -> list[Segment]:
    """把 LaTeX 源切成不重叠、全覆盖的 PROSE / PROTECTED 段序列。

    保证 ``"".join(s.text for s in result) == source``（往返无损）。
    """
    if not source:
        return []
    protected: list[tuple[int, int]] = []
    for pat in _PROTECTED_PATTERNS:
        for m in pat.finditer(source):
            if m.end() > m.start():
                protected.append((m.start(), m.end()))
    # 带参命令（含嵌套花括号）由平衡扫描器给出保护区间。
    for start, end in _command_protected_spans(source):
        if end > start:
            protected.append((start, end))
    protected = _merge_intervals(protected)

    segments: list[Segment] = []
    cursor = 0
    for start, end in protected:
        if start > cursor:
            segments.append(Segment(SegmentKind.PROSE, source[cursor:start]))
        segments.append(Segment(SegmentKind.PROTECTED, source[start:end]))
        cursor = end
    if cursor < len(source):
        segments.append(Segment(SegmentKind.PROSE, source[cursor:]))
    return segments


def _is_substantial_prose(text: str) -> bool:
    return len(_LETTER.findall(text)) >= _MIN_PROSE_LETTERS


class InplaceLatexPolisher:
    """LaTeX 原地润色器：保结构、只润散文，确定性守卫兜底。"""

    def __init__(self, llm: LLMProvider, *, is_mock: bool = False) -> None:
        self._llm = llm
        self._is_mock = is_mock

    def polish(self, source: str) -> InplacePolishResult:
        """润色整份 LaTeX 源，返回结构完全保留、仅散文被润色的新源。

        Mock provider（``is_mock=True``）下整体 no-op（输出逐字节等于输入）。
        """
        segments = segment_latex(source)
        # 保护：拼回必须无损（防御式自检，理论上恒成立）。
        if "".join(s.text for s in segments) != source:
            return InplacePolishResult(
                source=source,
                notes=["原地润色降级：分段自检失败，已原样返回。"],
            )

        candidates = [
            s for s in segments
            if s.kind == SegmentKind.PROSE and _is_substantial_prose(s.text)
        ]
        total = len(candidates)
        if self._is_mock or total == 0:
            return InplacePolishResult(
                source=source,
                total_prose_segments=total,
                notes=(["Mock provider：原地润色 no-op（逐字节不变）。"]
                       if self._is_mock else
                       ["无可润色的散文片段（可能整份文档均为结构/公式）。"]),
            )

        polished_count = 0
        rejected = 0
        out_parts: list[str] = []
        for seg in segments:
            if seg.kind != SegmentKind.PROSE or not _is_substantial_prose(seg.text):
                out_parts.append(seg.text)
                continue
            new_text = self._polish_fragment(seg.text)
            if new_text is not None and new_text != seg.text:
                out_parts.append(new_text)
                polished_count += 1
            else:
                out_parts.append(seg.text)
                if new_text is None:
                    rejected += 1

        notes = [
            f"原地润色：共 {total} 个散文片段，润色 {polished_count} 个，"
            f"守卫拦截保留原文 {rejected} 个；结构（公式/命令/图表/引用）逐字保留。"
        ]
        return InplacePolishResult(
            source="".join(out_parts),
            total_prose_segments=total,
            polished_segments=polished_count,
            rejected_by_guard=rejected,
            notes=notes,
        )

    def _polish_fragment(self, fragment: str) -> str | None:
        """润色单个散文片段：委托共享核心（保留前后空白、守卫兜底）。"""
        return polish_fragment(
            self._llm,
            lambda core: templates.polish_latex_prose(fragment=core),
            fragment,
            _LATEX_GUARD,
            preserve_edges=True,
        )


__all__ = [
    "Segment",
    "SegmentKind",
    "InplacePolishResult",
    "InplaceLatexPolisher",
    "segment_latex",
]
