"""Property-based tests for citation-faithfulness-audit · GroundingAssembler 组装阶段。

- Property 5: grounding-only 不变量（Req 2.1 / 2.4 / 3.1）——``assemble_grounding``
  产出的 grounding 每一段都取材于该 ``ReferenceEntry`` 的 ``title`` /
  ``abstract`` / ``abstract_sections``（去 join 分隔符后每个非空片段都是这些来源
  之一的子串）；修改被引文献之外的字段（authors / year / id / source_id /
  source / verified / pdf_url）不改变 grounding；组装是纯函数、不涉及任何 LLM。
- Property 7: 喂入判定器的文本受 token_budget 上限约束（Req 2.6 / 7.4）——
  对任意 ``ReferenceEntry`` 与任意正整数 ``token_budget``，
  ``len(assemble_grounding(ref, token_budget=budget)) <= budget``。

生成器约束（对齐既有 props 测试）：任意 ``st.text`` 一律排除 unicode 代理区 "Cs"
与控制字符 "Cc"。``ReferenceEntry`` 覆盖空/短/长 ``abstract``、有/无
``abstract_sections``（键混用已知段落名与任意文本，令 ``extract_section`` 可能命中
或不命中）。
"""

from __future__ import annotations

import inspect
from dataclasses import replace

from hypothesis import given, settings
from hypothesis import strategies as st

from paper_agent.tools.faithfulness_grounding import assemble_grounding
from paper_agent.workspace.models import ReferenceEntry

# --------------------------------------------------------------------------- #
# 生成器
# --------------------------------------------------------------------------- #

# 通用文本：排除代理区与控制字符（与既有 props 测试一致）。
_TEXT = st.text(
    alphabet=st.characters(blacklist_categories=("Cs", "Cc")),
    max_size=60,
)

# 段落名：混用 extract_section 认得的启发式关键段名与任意短文本，
# 让 abstract_sections 有时被结构化命中、有时不被命中。
_SECTION_NAME = st.one_of(
    st.sampled_from(["method", "results", "motivation", "conclusion"]),
    st.text(alphabet=st.characters(blacklist_categories=("Cs", "Cc")),
            min_size=1, max_size=10),
)

_ABSTRACT_SECTIONS = st.dictionaries(
    keys=_SECTION_NAME,
    values=_TEXT,
    max_size=4,
)


@st.composite
def _reference_entries(draw):
    """生成覆盖空/短/长 abstract、有/无 abstract_sections 的 ReferenceEntry。"""
    return ReferenceEntry(
        id=draw(st.text(alphabet="abcdefghijklmnop0123456789", min_size=1, max_size=8)),
        title=draw(_TEXT),
        authors=draw(st.lists(_TEXT, max_size=3)),
        year=draw(st.one_of(st.none(), st.integers(min_value=1900, max_value=2100))),
        source_id=draw(_TEXT),
        source=draw(st.text(alphabet="abcdefghijklmnop", max_size=10)),
        verified=draw(st.booleans()),
        abstract=draw(_TEXT),
        pdf_url=draw(_TEXT),
        abstract_sections=draw(_ABSTRACT_SECTIONS),
    )


def _allowed_sources(ref: ReferenceEntry) -> list[str]:
    """grounding 唯一合法取材来源：title + abstract + 各 abstract_sections 值。"""
    sources = [ref.title, ref.abstract]
    sources.extend(ref.abstract_sections.values())
    return sources


def _budget_no_truncation(ref: ReferenceEntry) -> int:
    """足以避免防御式截断的 token_budget：总来源长度 + 分隔符裕量。"""
    total = len(ref.title) + len(ref.abstract)
    total += sum(len(v) for v in ref.abstract_sections.values())
    # join 分隔符 "\n\n" 只在片段间加入，不超过来源总长；加常量裕量确保不截断。
    return total + 1000


# --------------------------------------------------------------------------- #
# Property 5: grounding-only 不变量
# --------------------------------------------------------------------------- #

# Feature: citation-faithfulness-audit, Property 5: grounding-only 不变量
@given(ref=_reference_entries())
@settings(max_examples=200)
def test_prop5_grounding_only_invariant(ref):
    """Validates: Requirements 2.1, 2.4, 3.1"""
    # 组装是纯函数、不涉及 LLM：签名里没有任何 provider/llm 参数（按构造保证）。
    params = set(inspect.signature(assemble_grounding).parameters)
    assert not (params & {"llm", "provider", "llm_provider", "model", "client"})

    # 用大预算避免截断干扰（截断单独由 Property 7 覆盖）。
    budget = _budget_no_truncation(ref)
    grounding = assemble_grounding(ref, token_budget=budget)

    sources = _allowed_sources(ref)

    # grounding 的每个非空片段（按 join 分隔符切开）都必须是某一合法来源的子串。
    for segment in grounding.split("\n\n"):
        seg = segment.strip()
        if not seg:
            continue
        assert any(seg in src for src in sources), (
            f"grounding 片段未取材于合法来源: {seg!r} not in any of {sources!r}"
        )

    # 修改被引文献之外的字段不改变 grounding（只留 title/abstract/abstract_sections）。
    mutated = replace(
        ref,
        id=ref.id + "X",
        authors=list(ref.authors) + ["新增作者"],
        year=(ref.year or 0) + 1,
        source_id=ref.source_id + "Y",
        source=ref.source + "Z",
        verified=not ref.verified,
        pdf_url=ref.pdf_url + "http://mutated",
    )
    mutated_grounding = assemble_grounding(mutated, token_budget=budget)
    assert mutated_grounding == grounding


# --------------------------------------------------------------------------- #
# Property 7: 喂入判定器的文本受 token_budget 上限约束
# --------------------------------------------------------------------------- #

# Feature: citation-faithfulness-audit, Property 7: 喂入判定器的文本受 token_budget 上限约束
@given(
    ref=_reference_entries(),
    token_budget=st.integers(min_value=1, max_value=500),
)
@settings(max_examples=200)
def test_prop7_grounding_respects_token_budget(ref, token_budget):
    """Validates: Requirements 2.6, 7.4"""
    grounding = assemble_grounding(ref, token_budget=token_budget)
    assert len(grounding) <= token_budget
