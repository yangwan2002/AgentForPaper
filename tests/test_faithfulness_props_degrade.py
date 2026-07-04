"""Property-based tests for citation-faithfulness-audit 安全降级语义。

- Property 6: grounding 不足即安全落 cannot_verify（Req 2.5）。
- Property 17: 单对异常隔离（Req 7.6）。

生成器约束：一律排除 unicode 代理区与控制字符（categories "Cs" / "Cc"）。
判定器以注入桩/间谍对象提供，读取被测编排的降级/隔离行为，绝不修改生产代码。
"""

from __future__ import annotations

from hypothesis import given, settings
from hypothesis import strategies as st

from paper_agent.agents.base import AgentContext
from paper_agent.agents.citation_faithfulness_agent import CitationFaithfulnessAgent
from paper_agent.tools.faithfulness_extract import extract_pairs
from paper_agent.tools.faithfulness_grounding import assemble_grounding
from paper_agent.workspace.faithfulness import FaithfulnessVerdict
from paper_agent.workspace.models import (
    InputMode,
    OutputFormat,
    PaperWorkspace,
    ParseStatus,
    ReferenceEntry,
    SectionDraft,
)

# --------------------------------------------------------------------------- #
# 生成器（排除 unicode 代理区 "Cs" 与控制字符 "Cc"，长度受限）
# --------------------------------------------------------------------------- #

_SAFE_TEXT = st.text(
    alphabet=st.characters(blacklist_categories=("Cs", "Cc")), max_size=40
)
_TINY_TEXT = st.text(
    alphabet=st.characters(blacklist_categories=("Cs", "Cc")), max_size=8
)
# 引用 id 仅取 ASCII 标识符子集，避免误捕获非引用方括号。
_ID_TEXT = st.text(
    alphabet="abcdefghijklmnopqrstuvwxyz0123456789", min_size=1, max_size=6
)

# 判定器不会在 reference_meta（title/year/authors）中出现的 sentinel。
_SENTINEL = "ZZRAISEZZ"
_TOKEN_BUDGET = 500


def _make_ws() -> PaperWorkspace:
    return PaperWorkspace(
        workspace_id="ws-degrade",
        input_mode=InputMode.GENERATION,
        output_format=OutputFormat.LATEX,
        topic_background="多智能体协作写作",
    )


def _apply(ws: PaperWorkspace, mutations) -> None:
    for mut in mutations:
        mut(ws)


def _pair_total(ws: PaperWorkspace) -> int:
    """用与被测代码相同的 extract_pairs 计算抽取对总数（含未验证对）。"""
    verified = ws.verified_reference_ids()
    total = 0
    for sid, draft in ws.section_drafts.items():
        verified_pairs, unverified_pairs = extract_pairs(
            sid, draft.content or "", verified
        )
        total += len(verified_pairs) + len(unverified_pairs)
    return total


class _SpyJudge:
    """间谍判定器：记录调用次数；被短路时应保持 calls == 0。"""

    def __init__(self) -> None:
        self.calls = 0

    def judge(self, *, claim, grounding, reference_meta):  # noqa: D401
        self.calls += 1
        return (FaithfulnessVerdict.SUPPORTED, "", "", ParseStatus.PARSED)


class _RaisingJudge:
    """桩判定器：对目标对抛异常，其余正常返回 supported。

    - ``raise_all=True``：对每一对都抛异常。
    - 否则：仅当 ``reference_meta`` 含 sentinel（目标文献 title 内嵌）时抛异常。
    """

    def __init__(self, *, raise_all: bool) -> None:
        self.raise_all = raise_all

    def judge(self, *, claim, grounding, reference_meta):  # noqa: D401
        if self.raise_all or (_SENTINEL in reference_meta):
            raise RuntimeError("judge boom")
        return (FaithfulnessVerdict.SUPPORTED, "", "", ParseStatus.PARSED)


# --------------------------------------------------------------------------- #
# Property 6: grounding 不足即安全落 cannot_verify
# --------------------------------------------------------------------------- #

@st.composite
def _tiny_grounding_workspace(draw) -> PaperWorkspace:
    """已验证文献 grounding 极短（含空）；章节引用这些文献产生已验证对。"""
    ref_ids = draw(st.lists(_ID_TEXT, min_size=1, max_size=4, unique=True))
    refs = [
        ReferenceEntry(
            id=rid,
            title=draw(_TINY_TEXT),
            authors=[],
            year=None,
            source_id=rid,
            source="arxiv",
            verified=True,
            abstract=draw(_TINY_TEXT),
            abstract_sections={},
        )
        for rid in ref_ids
    ]

    # 单章节引用全部已验证 id，确保存在已验证对（会被 grounding 短路）。
    content = " ".join(f"声明句 [{rid}]。" for rid in ref_ids)

    ws = _make_ws()
    ws.verified_references = refs
    ws.section_drafts = {"s0": SectionDraft(section_id="s0", title="T", content=content)}
    return ws


# Feature: citation-faithfulness-audit, Property 6: grounding 不足即安全落 cannot_verify
@given(ws=_tiny_grounding_workspace(), extra=st.integers(min_value=0, max_value=20))
@settings(max_examples=100)
def test_prop6_insufficient_grounding_short_circuits_cannot_verify(ws, extra):
    """Validates: Requirements 2.5"""
    # 计算每条已验证文献在同一 token_budget 下的 grounding 长度，
    # 令 min_grounding_chars 高于其最大值 → 所有已验证对都触发不足短路。
    lengths = [
        len(assemble_grounding(r, token_budget=_TOKEN_BUDGET))
        for r in ws.verified_references
    ]
    threshold = (max(lengths) if lengths else 0) + 1 + extra

    spy = _SpyJudge()
    agent = CitationFaithfulnessAgent(
        spy, min_grounding_chars=threshold, token_budget=_TOKEN_BUDGET
    )

    result = agent.run(AgentContext(workspace=ws))

    scratch = _make_ws()
    _apply(scratch, result.mutations)
    report = scratch.citation_faithfulness

    # 判定器绝不被调用（grounding 不足前置短路）。
    assert spy.calls == 0
    # 每条发现都落 cannot_verify。
    assert report, "预期至少存在一条已验证对发现"
    assert all(f["verdict"] == FaithfulnessVerdict.CANNOT_VERIFY.value for f in report)
    # 发现条数等于抽取对总数（未漏对）。
    assert len(report) == _pair_total(ws)


# --------------------------------------------------------------------------- #
# Property 17: 单对异常隔离
# --------------------------------------------------------------------------- #

@st.composite
def _judgeable_workspace(draw) -> tuple[PaperWorkspace, bool, set[str]]:
    """已验证文献 grounding 充足（title 非空）；章节仅引用已验证 id。

    返回 ``(ws, raise_all, raising_ids)``：``raising_ids`` 为判定器会抛异常的
    文献 id 集合（raise_all 时为全部，否则为单个内嵌 sentinel 的目标）。
    """
    ref_ids = draw(st.lists(_ID_TEXT, min_size=1, max_size=4, unique=True))
    raise_all = draw(st.booleans())
    target = draw(st.sampled_from(ref_ids))

    refs = []
    for rid in ref_ids:
        title = f"Title {rid}"
        if not raise_all and rid == target:
            title = f"Title {rid} {_SENTINEL}"
        refs.append(
            ReferenceEntry(
                id=rid,
                title=title,  # 非空 → grounding 非空 → 不被 grounding 短路
                authors=["A. Author"],
                year=2020,
                source_id=rid,
                source="arxiv",
                verified=True,
                abstract=f"Abstract {rid}",
            )
        )

    content = " ".join(f"声明句 [{rid}]。" for rid in ref_ids)

    ws = _make_ws()
    ws.verified_references = refs
    ws.section_drafts = {"s0": SectionDraft(section_id="s0", title="T", content=content)}

    raising_ids = set(ref_ids) if raise_all else {target}
    return ws, raise_all, raising_ids


# Feature: citation-faithfulness-audit, Property 17: 单对异常隔离
@given(bundle=_judgeable_workspace())
@settings(max_examples=100)
def test_prop17_single_pair_exception_isolation(bundle):
    """Validates: Requirements 7.6"""
    ws, raise_all, raising_ids = bundle

    judge = _RaisingJudge(raise_all=raise_all)
    # min_grounding_chars=0：不因 grounding 长度短路，保证判定器被调用。
    agent = CitationFaithfulnessAgent(
        judge, min_grounding_chars=0, token_budget=_TOKEN_BUDGET
    )

    # run 绝不向上抛异常（单对异常隔离），审计不被中止。
    result = agent.run(AgentContext(workspace=ws))

    scratch = _make_ws()
    _apply(scratch, result.mutations)
    report = scratch.citation_faithfulness

    # 报告总数不变：等于抽取对总数（异常对未被丢弃）。
    assert len(report) == _pair_total(ws)

    for f in report:
        if f["cited_reference_id"] in raising_ids:
            # 抛异常的对 → cannot_verify，且记录了降级原因。
            assert f["verdict"] == FaithfulnessVerdict.CANNOT_VERIFY.value
            assert f["rationale"] != ""
        else:
            # 其余对正常判定（返回 supported）。
            assert f["verdict"] == FaithfulnessVerdict.SUPPORTED.value
