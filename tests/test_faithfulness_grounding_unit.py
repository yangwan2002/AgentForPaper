"""单元测试：GroundingAssembler 复用 extract_section 命中段落（task 4.4）。

覆盖 Requirements 2.2 / 2.3：
- 2.2：段落抽取复用 ``paper_section_tool.extract_section``，grounding 直接采用其
  命中的段落文本（证明未新增第二套抽取实现）。
- 2.3：确定性拼接——title + 命中段落 + abstract 兜底都进入 grounding；仅有 title
  时 grounding 即 title；无任何可用来源时 grounding 为空字符串。
"""

from __future__ import annotations

from paper_agent.tools.faithfulness_grounding import assemble_grounding
from paper_agent.tools.paper_section_tool import extract_section
from paper_agent.workspace.models import ReferenceEntry

# 远大于任何测试文本长度的预算，确保断言不受防御式截断影响。
_LARGE_BUDGET = 100_000


def _make_ref(
    *,
    title: str = "",
    abstract: str = "",
    abstract_sections: dict[str, str] | None = None,
) -> ReferenceEntry:
    """构造仅填充测试关心字段的 ReferenceEntry（其余走默认值）。"""
    return ReferenceEntry(
        id="ref-1",
        title=title,
        authors=["A. Author"],
        year=2024,
        source_id="10.0000/example",
        abstract=abstract,
        abstract_sections=abstract_sections or {},
    )


def test_grounding_reuses_extract_section_matched_paragraph() -> None:
    """带结构化 method 段的 ref：grounding 含 extract_section 命中段落。

    "method" 是 assemble_grounding 默认 section_hints 之一，且与 abstract_sections
    的键精确匹配（小写比较）——extract_section 会返回该结构化段落原文。断言
    grounding 逐字包含该原文，证明 grounding 直接复用 extract_section 的命中结果，
    而非另起一套抽取逻辑。
    """
    section_text = (
        "We introduce a dual-encoder retrieval mechanism with contrastive "
        "alignment that jointly optimizes query and document towers."
    )
    # abstract 刻意不含 method/results/motivation/conclusion 关键词，避免启发式
    # 切片额外命中，从而让断言聚焦于「结构化段落被原样复用」。
    abstract_text = (
        "This paper studies large-scale semantic search over scholarly corpora "
        "and reports strong retrieval quality gains on held-out benchmarks."
    )
    title_text = "Dual-Encoder Retrieval for Scholarly Search"
    ref = _make_ref(
        title=title_text,
        abstract=abstract_text,
        abstract_sections={"method": section_text},
    )

    # 前置：extract_section 对命中的段落名返回结构化段落原文。
    matched = extract_section(ref, "method")
    assert matched == section_text

    grounding = assemble_grounding(ref, token_budget=_LARGE_BUDGET)

    # grounding 复用 extract_section 的命中段落（逐字包含），并含 title 与 abstract。
    assert matched in grounding
    assert title_text in grounding
    assert abstract_text in grounding


def test_grounding_title_only_ref_is_just_title() -> None:
    """仅有 title（无 abstract、无 abstract_sections）：grounding 恰为 title。"""
    title_text = "A Title Without Any Abstract"
    ref = _make_ref(title=title_text)

    grounding = assemble_grounding(ref, token_budget=_LARGE_BUDGET)

    assert grounding == title_text


def test_grounding_empty_when_no_usable_source() -> None:
    """无任何可用来源（空 title/abstract/sections）：grounding 为空字符串。"""
    ref = _make_ref(title="", abstract="", abstract_sections={})

    grounding = assemble_grounding(ref, token_budget=_LARGE_BUDGET)

    assert grounding == ""
