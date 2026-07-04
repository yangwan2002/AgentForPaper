"""Content_Contract：Normalized_Markdown 内容契约的规范化与校验。

本模块实现设计文档 format-pipeline-and-diff-revision 中 Content_Contract 组件的
两个纯函数，供导出管线、格式修复循环与写作智能体共享单一内容来源：

- ``normalize(content)`` —— 把输入归一化为受约束的 Normalized_Markdown 子集
  （段落 / ATX 标题 / 列表 / 强调 / 行内与围栏代码 / 行内 ``$...$`` 与块级
  ``$$...$$`` 数学 / 图片图表引用 / 表格 / 方括号文献引用 ``[id]``）。变换刻意
  保持最小且确定：仅统一行尾（``\\r\\n`` / ``\\r`` → ``\\n``）并去除受保护区块
  之外各行的行尾空白。**绝不静默丢弃内容**，也不对数学 / 代码定界符内部做任何
  改写（Req 5.4），并保证字节级幂等 ``normalize(normalize(x)) == normalize(x)``
  （Req 5.7）。

- ``validate(content, ws)`` —— 产出可诊断的 ``ContractViolation`` 列表：库外
  引用 ``[id]``（Req 5.5）、未唯一对应 ``FigureRecord`` 的图表引用（Req 5.6）、
  超过 1,000,000 字符（Req 5.8）、契约外构造（Req 5.9）。所有分支均保留原始内容
  （不截断、不丢弃），诊断项含字符偏移或行列位置与 ≤500 字符的出错片段。

内容契约受约束子集（Req 5.1）与本模块识别的语法约定：
- 文献引用：正文中形如 ``[id]`` 的方括号标注（``id`` 限 ASCII 标识符字符，
  与 ``tools/quality_gate.py`` 的 ``extract_text_citations`` 同一字符集，避免误
  捕获含空格 / CJK 的方括号）。紧邻 ``!`` 之前的 ``[...]``（图片 alt）不计为引用。
- 图表引用：Markdown 图片语法 ``![alt](figure_id)`` 的目标，或显式占位
  ``[figure:figure_id]`` / ``[fig:figure_id]``；被引用的 ``figure_id`` 必须唯一
  对应工作区中一条 ``FigureRecord``（Req 5.6）。
- 数学：行内 ``$...$`` 与块级 ``$$...$$``；其内部内容视为受保护区，不参与引用 /
  图表 / 构造扫描，也不被规范化改写（Req 5.4）。
- 代码：行内 `` `...` `` 与围栏 ``` ```...``` ```；同样为受保护区。

_Requirements: 5.1, 5.4, 5.5, 5.6, 5.7, 5.8, 5.9_
"""

from __future__ import annotations

import re
from collections import Counter
from typing import TYPE_CHECKING

from paper_agent.export.format_models import ContractViolation, NormalizeResult

if TYPE_CHECKING:  # 避免运行期不必要的耦合；仅供类型标注。
    from paper_agent.workspace.models import PaperWorkspace


# --------------------------------------------------------------------------- #
# 契约常量
# --------------------------------------------------------------------------- #

#: 单个 ``Section_Draft.content`` 的最大 Unicode 字符数（Req 5.8）。
MAX_CONTENT_CHARS = 1_000_000

#: 诊断项出错片段的最大长度（Req 5.9 / 9.4）。
EXCERPT_MAX_CHARS = 500


# --------------------------------------------------------------------------- #
# 语法识别正则
# --------------------------------------------------------------------------- #

# 正文里形如 [id] 的引用标注；id 限 ASCII 标识符字符（含冒号 / 点 / 连字符 /
# 下划线），与 quality_gate.extract_text_citations 保持同一字符集，避免误捕获
# 形如 [表格 第1页 #1] 这类含空格 / CJK 的非引用方括号。
_CITATION_RE = re.compile(r"\[([A-Za-z0-9_.:\-]+)\]")

# Markdown 图片语法 ![alt](target)；target 作为图表引用（figure_id）。
_IMAGE_RE = re.compile(r"!\[[^\]\n]*\]\(([^)\n]*)\)")

# 显式图表占位引用 [figure:ID] / [fig:ID]。
_FIGURE_REF_RE = re.compile(r"\[(?:figure|fig):([A-Za-z0-9_.\-]+)\]")

# 契约子集之外的原始 HTML 标签（如 <div> / <br/> / </span>）——用作
# "契约外构造" 的具体、可诊断检测；受保护区（代码 / 数学）内不计。
_HTML_RE = re.compile(r"</?[A-Za-z][^>\n]*>")

# 块级数学 $$...$$（跨行）。
_BLOCK_MATH_RE = re.compile(r"\$\$.*?\$\$", re.DOTALL)

# 行内代码 `...`（单行内）。
_INLINE_CODE_RE = re.compile(r"`[^`\n]*`")

# 行内数学 $...$（单行内，非空）。
_INLINE_MATH_RE = re.compile(r"\$[^$\n]+\$")


# --------------------------------------------------------------------------- #
# 4.1 normalize
# --------------------------------------------------------------------------- #


def normalize(content: str) -> NormalizeResult:
    """把 ``content`` 归一化为受约束的 Normalized_Markdown（Req 5.1）。

    变换保持最小且确定，以保证字节级幂等（Req 5.7）且绝不静默丢弃内容：

    1. 统一行尾：``\\r\\n`` 与孤立 ``\\r`` 均归一为 ``\\n``。
    2. 去除受保护区块（围栏代码 / 块级数学）之外各行的行尾空白（空格 / 制表符）。
       受保护区块内部逐字节保留——数学定界符内部不施加任何改写（Req 5.4）。

    返回 :class:`NormalizeResult`，``changed`` 标记是否发生改写；``violations``
    留空（契约违规诊断由 :func:`validate` 负责）。
    """
    original = content if content is not None else ""
    text = _normalize_line_endings(original)
    text = _strip_trailing_whitespace(text)
    return NormalizeResult(content=text, violations=[], changed=(text != original))


def _normalize_line_endings(text: str) -> str:
    """``\\r\\n`` / ``\\r`` → ``\\n``（幂等：无 ``\\r`` 时为恒等变换）。"""
    return text.replace("\r\n", "\n").replace("\r", "\n")


def _strip_trailing_whitespace(text: str) -> str:
    """去除受保护区块之外各行的行尾空格 / 制表符。

    以逐行状态机跟踪围栏代码块（``` / ~~~）与独占一行的块级数学定界符
    (``$$``)；处于其内部的行逐字节保留（Req 5.4，代码 / 数学内部不改写）。
    区块判定仅依据行首标记，不受行尾空白影响，故二次调用结果不变（幂等）。
    """
    lines = text.split("\n")
    out: list[str] = []
    in_code = False
    code_fence = ""
    in_block_math = False
    for line in lines:
        stripped = line.strip()
        if in_code:
            # 代码块内部：仅在遇到匹配的收尾围栏时退出，其余行原样保留。
            out.append(line)
            if stripped[:3] == code_fence and set(stripped) <= set(code_fence):
                in_code = False
                code_fence = ""
            continue
        if in_block_math:
            out.append(line)
            if stripped == "$$":
                in_block_math = False
            continue
        # 非受保护状态：先判定是否进入代码 / 块级数学区块。
        if stripped[:3] in ("```", "~~~"):
            in_code = True
            code_fence = stripped[:3]
            out.append(line.rstrip(" \t"))
            continue
        if stripped == "$$":
            in_block_math = True
            out.append(line.rstrip(" \t"))
            continue
        # 普通行：去除行尾空白（空格 / 制表符）。
        out.append(line.rstrip(" \t"))
    return "\n".join(out)


# --------------------------------------------------------------------------- #
# 4.2 validate
# --------------------------------------------------------------------------- #


def validate(content: str, ws: "PaperWorkspace") -> list[ContractViolation]:
    """校验 ``content`` 是否符合 Content_Contract，返回可诊断违规列表。

    检查项（均保留原始内容，绝不截断 / 丢弃）：

    - Req 5.8：长度 > 1,000,000 字符 → ``length_exceeded``（不截断）。
    - Req 5.5：引用 ``[id]`` 不在 ``ws.verified_reference_ids()`` → ``unknown_citation``
      （保留原文）。
    - Req 5.6：图表引用 ``figure_id`` 未唯一对应一条 ``FigureRecord``（0 或多重）
      → ``unknown_figure``。
    - Req 5.9：契约外构造（此处检测原始 HTML 标签）→ ``unknown_construct``
      （含字符偏移 / 行列位置与 ≤500 字符出错片段）。

    数学 / 代码定界符内部内容视为受保护区，不参与上述扫描（Req 5.4）。
    """
    text = content if content is not None else ""
    violations: list[ContractViolation] = []

    # Req 5.8：长度上限——诊断但不截断。
    if len(text) > MAX_CONTENT_CHARS:
        line, column = _location(text, MAX_CONTENT_CHARS)
        violations.append(
            ContractViolation(
                kind="length_exceeded",
                message=(
                    f"内容长度 {len(text)} 字符，超过上限 {MAX_CONTENT_CHARS} 字符；"
                    f"保留原始内容，未截断。"
                ),
                offset=MAX_CONTENT_CHARS,
                line=line,
                column=column,
                excerpt=_excerpt(text, MAX_CONTENT_CHARS),
            )
        )

    protected = _protected_mask(text)
    verified_ids = ws.verified_reference_ids() if ws is not None else set()
    figure_counts: Counter[str] = Counter(
        f.figure_id for f in getattr(ws, "figures", []) or []
    )

    # 先收集图表引用位置，便于把这些方括号从引用扫描中排除。
    figure_ref_spans: set[int] = set()

    # Req 5.6：图表引用唯一对应 FigureRecord。
    for m in _IMAGE_RE.finditer(text):
        if protected[m.start()]:
            continue
        ref = m.group(1).strip()
        off = m.start(1)
        if figure_counts.get(ref, 0) != 1:
            violations.append(_figure_violation(text, ref, off, figure_counts.get(ref, 0)))
    for m in _FIGURE_REF_RE.finditer(text):
        if protected[m.start()]:
            continue
        figure_ref_spans.add(m.start())
        ref = m.group(1)
        off = m.start(1)
        if figure_counts.get(ref, 0) != 1:
            violations.append(_figure_violation(text, ref, off, figure_counts.get(ref, 0)))

    # Req 5.5：库外引用 [id] —— 诊断并保留原文。
    for m in _CITATION_RE.finditer(text):
        start = m.start()
        if protected[start]:
            continue  # 代码 / 数学内部的方括号不是引用
        if start > 0 and text[start - 1] == "!":
            continue  # 图片 alt：![...] 不是引用
        if start in figure_ref_spans:
            continue  # [figure:ID] / [fig:ID] 是图表引用，已单独处理
        cid = m.group(1)
        if cid.startswith("figure:") or cid.startswith("fig:"):
            continue
        if cid not in verified_ids:
            line, column = _location(text, start)
            violations.append(
                ContractViolation(
                    kind="unknown_citation",
                    message=f"引用 [{cid}] 不在已验证文献库中；保留原文。",
                    offset=start,
                    line=line,
                    column=column,
                    excerpt=_excerpt(text, start),
                )
            )

    # Req 5.9：契约外构造（原始 HTML 标签）—— 诊断且绝不静默丢弃。
    for m in _HTML_RE.finditer(text):
        start = m.start()
        if protected[start]:
            continue  # 代码 / 数学内部允许出现 < >
        line, column = _location(text, start)
        violations.append(
            ContractViolation(
                kind="unknown_construct",
                message=(
                    f"检测到契约子集之外的构造（原始 HTML）：{_excerpt(text, start, 60)!r}；"
                    f"保留原文，未丢弃。"
                ),
                offset=start,
                line=line,
                column=column,
                excerpt=_excerpt(text, start),
            )
        )

    return violations


def _figure_violation(
    text: str, ref: str, offset: int, count: int
) -> ContractViolation:
    line, column = _location(text, offset)
    if count == 0:
        detail = "未对应任何 FigureRecord"
    else:
        detail = f"对应 {count} 条 FigureRecord（非唯一）"
    return ContractViolation(
        kind="unknown_figure",
        message=f"图表引用 figure_id={ref!r} {detail}；需唯一对应一条 FigureRecord。",
        offset=offset,
        line=line,
        column=column,
        excerpt=_excerpt(text, offset),
    )


# --------------------------------------------------------------------------- #
# 受保护区（代码 / 数学）标记与定位辅助
# --------------------------------------------------------------------------- #


def _protected_mask(text: str) -> bytearray:
    """返回逐字符掩码：1 表示该字符处于受保护区（代码 / 数学）内部。

    覆盖围栏代码块、块级数学 ``$$...$$``、行内代码 `` `...` ``、行内数学
    ``$...$``；后者仅在其起点未落入前者已标记区时才生效，避免重复 / 交叠误判。
    """
    n = len(text)
    mask = bytearray(n)

    def _mark(start: int, end: int) -> None:
        if end > start:
            mask[start:end] = b"\x01" * (end - start)

    # 围栏代码块（逐行，含未闭合块直至文末）。
    for start, end in _fenced_code_spans(text):
        _mark(start, end)
    # 块级数学 $$...$$。
    for m in _BLOCK_MATH_RE.finditer(text):
        if not mask[m.start()]:
            _mark(*m.span())
    # 行内代码 `...`。
    for m in _INLINE_CODE_RE.finditer(text):
        if not mask[m.start()]:
            _mark(*m.span())
    # 行内数学 $...$。
    for m in _INLINE_MATH_RE.finditer(text):
        if not mask[m.start()]:
            _mark(*m.span())
    return mask


def _fenced_code_spans(text: str) -> list[tuple[int, int]]:
    """返回围栏代码块的 (start, end) 字符区间列表（end 为排他）。"""
    spans: list[tuple[int, int]] = []
    offset = 0
    in_code = False
    fence = ""
    code_start = 0
    lines = text.split("\n")
    for idx, line in enumerate(lines):
        line_start = offset
        line_end = offset + len(line)
        stripped = line.strip()
        marker = stripped[:3]
        if not in_code and marker in ("```", "~~~"):
            in_code = True
            fence = marker
            code_start = line_start
        elif in_code and marker == fence and set(stripped) <= set(fence):
            in_code = False
            spans.append((code_start, line_end))
        # 推进偏移：+1 补回 split 掉的 '\n'（最后一行不加，但不影响区间）。
        offset = line_end + 1
    if in_code:
        spans.append((code_start, len(text)))
    return spans


def _location(text: str, offset: int) -> tuple[int, int]:
    """把字符偏移换算为 1 基的 (行, 列)。越界时钳制到文本末尾。"""
    if offset < 0:
        offset = 0
    if offset > len(text):
        offset = len(text)
    prefix = text[:offset]
    line = prefix.count("\n") + 1
    last_nl = prefix.rfind("\n")
    column = offset - last_nl  # last_nl == -1 时恰为 offset + 1
    return line, column


def _excerpt(text: str, offset: int, length: int = EXCERPT_MAX_CHARS) -> str:
    """从 ``offset`` 起截取不超过 ``length`` 字符（且 ≤500）的出错片段。"""
    if offset < 0:
        offset = 0
    limit = min(length, EXCERPT_MAX_CHARS)
    return text[offset : offset + limit]


__all__ = [
    "MAX_CONTENT_CHARS",
    "EXCERPT_MAX_CHARS",
    "normalize",
    "validate",
]
