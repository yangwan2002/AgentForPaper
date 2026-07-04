"""format-pipeline-and-diff-revision 内容契约与导出属性测试（Property 9–16）。

每条 Correctness Property 用单个 Hypothesis 属性测试实现（max_examples=100），
直接驱动 Content_Contract（normalize / validate）、MarkdownExporter 与
LatexExporter；不依赖真实 pandoc/pdflatex（pandoc 缺失时导出走 fallback，
Markdown 导出器根本不触碰 subprocess），保证测试可重复且无网络/工具依赖。
"""

from __future__ import annotations

import os
import subprocess
import tempfile

from hypothesis import assume, given, settings
from hypothesis import strategies as st

from paper_agent.export.content_contract import (
    MAX_CONTENT_CHARS,
    normalize,
    validate,
)
from paper_agent.export.latex import LatexExporter
from paper_agent.export.markdown import MarkdownExporter
from paper_agent.workspace.models import (
    FigureRecord,
    InputMode,
    OutlineNode,
    OutputFormat,
    PaperWorkspace,
    ReferenceEntry,
    SectionDraft,
)


# --------------------------------------------------------------------------- #
# 公共构造工具
# --------------------------------------------------------------------------- #


def _empty_ws() -> PaperWorkspace:
    """无文献、无图表的最小工作区（供 normalize/validate 属性）。"""
    return PaperWorkspace(
        workspace_id="w", input_mode=InputMode.GENERATION, topic_background="x"
    )


def _ref(i: int) -> ReferenceEntry:
    return ReferenceEntry(
        id=f"ref{i}",
        title=f"Title{i}",
        authors=[f"Auth{i}"],
        year=2000 + i,
        source_id=f"id{i}",
        source="s",
        verified=True,
    )


def _export_ws(
    sections: list[tuple[str, str]],
    refs: list[ReferenceEntry],
    figures: list[FigureRecord],
    fmt: OutputFormat,
) -> PaperWorkspace:
    ws = PaperWorkspace(
        workspace_id="paper",
        input_mode=InputMode.GENERATION,
        output_format=fmt,
        topic_background="x",
    )
    for order, (title, content) in enumerate(sections):
        sid = f"sec{order}"
        ws.outline.append(OutlineNode(section_id=sid, title=title, order=order))
        ws.section_drafts[sid] = SectionDraft(
            section_id=sid, title=title, content=content
        )
    ws.verified_references = refs
    ws.figures = figures
    return ws


# --------------------------------------------------------------------------- #
# Property 9: normalize 字节级幂等
# --------------------------------------------------------------------------- #

_P9_FRAGMENTS = [
    "\r\n",
    "\r",
    "\n",
    "trailing   ",
    "\t\t",
    "$x^2$",
    "$$",
    "$$\n\\sum_i x_i\n$$",
    "```",
    "```\ncode  \n```",
    "# 标题  ",
    "- 列表项  ",
    "`inline`",
    "[id]",
    "你好世界",
    "abc",
    "  leading",
]

_p9_content = st.lists(
    st.one_of(
        st.sampled_from(_P9_FRAGMENTS),
        st.text(max_size=6),
    ),
    max_size=30,
).map("".join)


# Feature: format-pipeline-and-diff-revision, Property 9: normalize 字节级幂等
@settings(max_examples=100)
@given(x=_p9_content)
def test_p9_normalize_byte_idempotent(x):
    once = normalize(x).content
    twice = normalize(once).content
    assert twice == once


# --------------------------------------------------------------------------- #
# Property 10: 归一化保留内容、绝不静默丢弃
# --------------------------------------------------------------------------- #

# 前缀字符集刻意排除会构成契约语法（数学/代码/引用/图片/HTML/标题）的字符，
# 保证注入的 <div> 与 [unkref] 是文本中仅有的、可预期的契约违规触发点。
_p10_prefix = st.text(
    alphabet=st.characters(blacklist_characters="<>[]$`()!#~\r\\"),
    max_size=40,
)


# Feature: format-pipeline-and-diff-revision, Property 10: 归一化保留内容、绝不静默丢弃
@settings(max_examples=100)
@given(prefix=_p10_prefix, oversize=st.booleans())
def test_p10_normalize_preserves_and_never_drops(prefix, oversize):
    ws = _empty_ws()
    unknown_id = "unkref"
    body = prefix + " <div>raw</div> [" + unknown_id + "]"
    if oversize:
        body = body + ("a" * (MAX_CONTENT_CHARS + 1))

    violations = validate(body, ws)
    kinds = {v.kind for v in violations}

    # 契约外构造（原始 HTML）与库外引用均以可诊断项标识，绝不静默丢弃。
    assert "unknown_construct" in kinds
    assert "unknown_citation" in kinds
    if oversize:
        # 超过 1,000,000 字符 → 诊断但不截断。
        assert "length_exceeded" in kinds

    # 每条诊断均可定位（含字符偏移或行列位置）。
    for v in violations:
        assert v.offset is not None or (v.line is not None and v.column is not None)

    # 原始内容既不被 validate 改动，规范化后也保留不合规子串（不丢弃、不截断）。
    normalized = normalize(body).content
    assert "<div>raw</div>" in normalized
    assert "[" + unknown_id + "]" in normalized


# --------------------------------------------------------------------------- #
# Property 11: 产物符合内容契约
# --------------------------------------------------------------------------- #

_ALLOWED_FRAGMENTS = [
    "# 标题",
    "## 二级标题",
    "- 列表项一",
    "1. 有序项",
    "*强调*",
    "_斜体_",
    "`行内代码`",
    "```\ncode block\n```",
    "$a + b$",
    "$$\n\\sum_i x_i\n$$",
    "| a | b |\n| - | - |\n| 1 | 2 |",
    "[ref1]",
    "普通段落文本 hello 你好",
]

_p11_content = st.lists(
    st.one_of(
        st.sampled_from(_ALLOWED_FRAGMENTS),
        st.text(
            alphabet=st.characters(blacklist_characters="<>"),
            max_size=8,
        ),
    ),
    max_size=20,
).map(lambda parts: "\n\n".join(parts))


# Feature: format-pipeline-and-diff-revision, Property 11: 产物符合内容契约
@settings(max_examples=100)
@given(content=_p11_content)
def test_p11_product_conforms_to_contract(content):
    ws = _empty_ws()
    normalized = normalize(content).content
    violations = validate(normalized, ws)
    # 仅由受约束子集构造 → 不产生 unknown_construct 诊断。
    assert not any(v.kind == "unknown_construct" for v in violations)


# --------------------------------------------------------------------------- #
# Property 12: 数学定界符内部不被破坏
# --------------------------------------------------------------------------- #


@st.composite
def _math_content(draw):
    """生成内嵌行内 / 块级数学的内容，并返回应逐字节保留的数学子串。"""
    block = draw(st.booleans())
    if block:
        # 块级数学：内部行不含 '$'（避免提前闭合）与 '\r'（行尾归一影响）。
        interior_lines = draw(
            st.lists(
                st.text(
                    alphabet=st.characters(blacklist_characters="$\r\n"),
                    max_size=12,
                ),
                min_size=1,
                max_size=3,
            )
        )
        interior = "\n".join(interior_lines)
        math = "$$\n" + interior + "\n$$"
        content = "前置段落\n\n" + math + "\n\n后置段落"
    else:
        # 行内数学：内部非空、不含 '$' / 换行 / '\r'。
        interior = draw(
            st.text(
                alphabet=st.characters(blacklist_characters="$\n\r"),
                min_size=1,
                max_size=16,
            )
        )
        math = "$" + interior + "$"
        content = "lead " + math + " tail"
    return content, math


# Feature: format-pipeline-and-diff-revision, Property 12: 数学定界符内部不被破坏
@settings(max_examples=100)
@given(_math_content())
def test_p12_math_delimiters_interior_preserved(data):
    content, math = data
    normalized = normalize(content).content
    # 数学定界符与其内部符号逐字节保留，而非被逐字符转义为纯文本。
    assert math in normalized


# --------------------------------------------------------------------------- #
# Property 13: figure 引用唯一对应
# --------------------------------------------------------------------------- #

_fig_id = st.text(
    alphabet="abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_.-",
    min_size=1,
    max_size=12,
)


# Feature: format-pipeline-and-diff-revision, Property 13: figure 引用唯一对应
@settings(max_examples=100)
@given(fid=_fig_id, count=st.sampled_from([0, 1, 2]))
def test_p13_figure_reference_unique_correspondence(fid, count):
    ws = _empty_ws()
    ws.figures = [
        FigureRecord(figure_id=fid, data_ref=f"data{i}.png", caption="c")
        for i in range(count)
    ]
    content = f"见 [figure:{fid}] 所示。"

    violations = validate(content, ws)
    unknown_figures = [v for v in violations if v.kind == "unknown_figure"]

    if count == 1:
        # 唯一对应一条 FigureRecord → 无 unknown_figure 诊断。
        assert unknown_figures == []
    else:
        # 零或多重对应 → 以可诊断项标识，且可定位。
        assert unknown_figures
        for v in unknown_figures:
            assert v.offset is not None or (v.line is not None and v.column is not None)


# --------------------------------------------------------------------------- #
# Property 14: Markdown 直接渲染保真且零外部依赖
# --------------------------------------------------------------------------- #


@st.composite
def _md_workspace(draw):
    n = draw(st.integers(min_value=0, max_value=4))
    sections: list[tuple[str, str]] = []
    for i in range(n):
        title = f"Section{i}"
        suffix = draw(
            st.text(
                # 排除代理/控制字符（Cs/Cc）：孤立代理无法 UTF-8 落盘，且真实
                # 章节正文不会包含它们——这是生成器约束而非产品缺陷。
                alphabet=st.characters(
                    blacklist_characters="#<>\r",
                    blacklist_categories=("Cs", "Cc"),
                ),
                max_size=10,
            )
        )
        content = f"Body{i} $x_{i}$ `code{i}` [ref{i}] {suffix}"
        sections.append((title, content))
    m = draw(st.integers(min_value=0, max_value=4))
    refs = [_ref(i) for i in range(m)]
    return sections, refs


# Feature: format-pipeline-and-diff-revision, Property 14: Markdown 直接渲染保真且零外部依赖
@settings(max_examples=100)
@given(_md_workspace())
def test_p14_markdown_direct_render_no_external_deps(data):
    sections, refs = data
    ws = _export_ws(sections, refs, [], OutputFormat.MARKDOWN)

    calls: list = []
    orig_run = subprocess.run
    subprocess.run = lambda *a, **k: calls.append((a, k))  # noqa: E731
    try:
        with tempfile.TemporaryDirectory() as d:
            result = MarkdownExporter().export(ws, d)
            assert len(result.files) == 1
            path = result.files[0]
            assert os.path.exists(path)
            text = open(path, encoding="utf-8").read()
    finally:
        subprocess.run = orig_run

    # 零外部依赖：从不调用任何外部可执行程序，且不标注降级。
    assert calls == []
    assert result.notes == []

    # 引用闭合（agent-reliability-and-subagents Property 2）：参考文献表只列被正文
    # 实际引用的文献。section i 的正文含 `[ref{i}]`，故 ref{i} 被引用当且仅当存在
    # 第 i 个 section。保持 verified_references 既定顺序。
    cited = [r for r in refs if any(f"[{r.id}]" in content for _, content in sections)]

    if not sections and not cited:
        # 空章节集合且无被引用文献 → 仅产出结构骨架 <id>.md。
        assert f"# {ws.workspace_id}" in text
        return

    # 章节顺序 + 标题层级：各 `# SectionI` 按序出现。
    positions = [text.index(f"# {title}") for title, _ in sections]
    assert positions == sorted(positions)

    # 正文字节保真：数学 / 代码 / [id] 原样保留、不转义（正文契约不受引用闭合影响）。
    for i, (_title, content) in enumerate(sections):
        assert content in text
        assert f"$x_{i}$" in text
        assert f"`code{i}`" in text
        assert f"[ref{i}]" in text

    # 参考文献：仅被引用文献，按既定顺序连续重新编号。
    for idx, r in enumerate(cited):
        expected = f"{idx + 1}. {r.authors[0]} ({r.year}). {r.title}. {r.source}:{r.source_id}"
        assert expected in text
    # 未被引用的已验证文献不进入参考文献表。
    uncited = [r for r in refs if r not in cited]
    for r in uncited:
        assert f"). {r.title}. {r.source}:{r.source_id}" not in text


# --------------------------------------------------------------------------- #
# Property 15: .bib 恰为已验证文献集合
# --------------------------------------------------------------------------- #


# Feature: format-pipeline-and-diff-revision, Property 15: .bib 恰为被正文引用的文献集合
# （引用闭合升级，见 agent-reliability-and-subagents Property 2）：正文只引用 [ref0]，
# 故 .bib 只含被引用文献，未被引用的已验证文献不进入 .bib。
@settings(max_examples=100)
@given(m=st.integers(min_value=0, max_value=5))
def test_p15_bib_equals_verified_reference_set(m):
    refs = [_ref(i) for i in range(m)]
    ws = _export_ws([("Intro", "正文 [ref0]")], refs, [], OutputFormat.LATEX)

    with tempfile.TemporaryDirectory() as d:
        result = LatexExporter().export(ws, d)
        bib_paths = [f for f in result.files if f.endswith(".bib")]
        assert len(bib_paths) == 1
        bib = open(bib_paths[0], encoding="utf-8").read()

    # 正文仅引用 ref0：被引用集合大小为 min(1, m)。
    cited_count = min(1, m)
    assert bib.count("@article{") == cited_count
    if m >= 1:
        assert refs[0].title in bib
    # 未被引用的已验证文献不进入 .bib。
    for r in refs[1:]:
        assert r.title not in bib


# --------------------------------------------------------------------------- #
# Property 16: 导出产物路径均存在
# --------------------------------------------------------------------------- #


# Feature: format-pipeline-and-diff-revision, Property 16: 导出产物路径均存在
@settings(max_examples=100)
@given(
    n=st.integers(min_value=1, max_value=3),
    m=st.integers(min_value=0, max_value=3),
    fmt=st.sampled_from([OutputFormat.MARKDOWN, OutputFormat.LATEX]),
)
def test_p16_export_result_files_exist(n, m, fmt):
    sections = [(f"Sec{i}", f"正文{i} [ref0]") for i in range(n)]
    refs = [_ref(i) for i in range(m)]
    ws = _export_ws(sections, refs, [], fmt)
    exporter = (
        MarkdownExporter() if fmt is OutputFormat.MARKDOWN else LatexExporter()
    )

    with tempfile.TemporaryDirectory() as d:
        result = exporter.export(ws, d)
        # 成功导出的每个产物路径均在文件系统中实际存在。
        for path in result.files:
            assert os.path.exists(path)
