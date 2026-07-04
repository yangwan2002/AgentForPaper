"""Property-based tests for citation-faithfulness-audit 序列化与替换语义。

- Property 13: 报告序列化 round-trip 与向后兼容默认（Req 5.3 / 5.4 / 9.5）。
- Property 14: 再次运行替换而非累加（Req 5.5 / 9.5）。

生成器约束：写入磁盘 / 参与序列化的字符串一律排除 unicode 代理区与控制字符
（categories "Cs" / "Cc"），避免代理/控制字符导致的序列化失败。
"""

from __future__ import annotations

from hypothesis import given, settings
from hypothesis import strategies as st

from paper_agent.agents.base import AgentContext
from paper_agent.agents.citation_faithfulness_agent import (
    CitationFaithfulnessAgent,
    FaithfulnessJudge,
)
from paper_agent.parsing.structured_parser import ParseOutcome
from paper_agent.tools.faithfulness_extract import extract_pairs
from paper_agent.workspace.faithfulness import (
    CitationFaithfulnessFinding,
    FaithfulnessVerdict,
)
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
# 引用 id 仅取 quality_gate._TEXT_CITATION 认可的 ASCII 标识符字符子集。
_ID_TEXT = st.text(
    alphabet="abcdefghijklmnopqrstuvwxyz0123456789", min_size=1, max_size=6
)
_VERDICTS = st.sampled_from(list(FaithfulnessVerdict))
_SEVERITIES = st.sampled_from(["high", "medium", "low", "none"])
_PARSE_STATES = st.sampled_from(["", "parsed", "n/a", "failed", "mock_fallback"])


@st.composite
def _finding(draw) -> CitationFaithfulnessFinding:
    """生成一条可序列化的忠实性发现。"""
    return CitationFaithfulnessFinding(
        section_id=draw(_SAFE_TEXT),
        cited_reference_id=draw(_SAFE_TEXT),
        claim_excerpt=draw(_SAFE_TEXT),
        verdict=draw(_VERDICTS),
        severity=draw(_SEVERITIES),
        rationale=draw(_SAFE_TEXT),
        supporting_snippet=draw(_SAFE_TEXT),
        parse_status=draw(_PARSE_STATES),
        unverified_reference=draw(st.booleans()),
    )


@st.composite
def _workspace(draw) -> PaperWorkspace:
    """生成含随机章节正文与已验证文献的最小工作区。"""
    ref_ids = draw(st.lists(_ID_TEXT, min_size=0, max_size=4, unique=True))
    extra_ids = draw(st.lists(_ID_TEXT, min_size=0, max_size=2, unique=True))
    # 可被引用的 id 集合：已验证 + 若干未验证（触发 unverified 对）。
    citeable = list(ref_ids) + [e for e in extra_ids if e not in ref_ids]

    section_drafts: dict[str, SectionDraft] = {}
    n_sections = draw(st.integers(min_value=0, max_value=3))
    for i in range(n_sections):
        prefix = draw(_SAFE_TEXT)
        cited = (
            draw(st.lists(st.sampled_from(citeable), max_size=4)) if citeable else []
        )
        parts = [prefix]
        for cid in cited:
            parts.append(f" 声明句 [{cid}]。")
        sid = f"s{i}"
        section_drafts[sid] = SectionDraft(
            section_id=sid, title=f"T{i}", content="".join(parts)
        )

    refs = [
        ReferenceEntry(
            id=rid,
            title=f"Title {rid}",
            authors=["A. Author"],
            year=2020,
            source_id=rid,
            source="arxiv",
            verified=True,
            abstract=f"Abstract for {rid}.",
        )
        for rid in ref_ids
    ]

    ws = PaperWorkspace(
        workspace_id="w",
        input_mode=InputMode.GENERATION,
        output_format=OutputFormat.MARKDOWN,
        topic_background="t",
    )
    ws.verified_references = refs
    ws.section_drafts = section_drafts
    return ws


class _StubParser:
    """桩 StructuredParser：始终返回 PARSED（supported），供判定器读取。"""

    def request_json(self, messages, *, required_keys=()) -> ParseOutcome:  # noqa: D401
        return ParseOutcome(
            status=ParseStatus.PARSED,
            data={"verdict": "supported", "rationale": "", "supporting_snippet": ""},
        )


def _make_ws() -> PaperWorkspace:
    return PaperWorkspace(
        workspace_id="ws1",
        input_mode=InputMode.GENERATION,
        output_format=OutputFormat.LATEX,
        topic_background="多智能体协作写作",
    )


def _make_agent() -> CitationFaithfulnessAgent:
    # min_grounding_chars=0：不因 grounding 长度短路误判，保证发现条数=抽取对总数。
    return CitationFaithfulnessAgent(
        FaithfulnessJudge(_StubParser()), min_grounding_chars=0, token_budget=200
    )


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


def _apply(ws: PaperWorkspace, mutations) -> None:
    for mut in mutations:
        mut(ws)


# --------------------------------------------------------------------------- #
# Property 13: 报告序列化 round-trip 与向后兼容默认
# --------------------------------------------------------------------------- #

# Feature: citation-faithfulness-audit, Property 13: 报告序列化 round-trip 与向后兼容默认
@given(findings=st.lists(_finding(), max_size=8))
@settings(max_examples=100)
def test_prop13_report_serialization_roundtrip_and_backcompat(findings):
    """Validates: Requirements 5.3, 5.4, 9.5"""
    expected_dicts = [f.to_dict() for f in findings]

    ws = _make_ws()
    ws.citation_faithfulness = list(expected_dicts)

    # to_dict -> from_dict 往返后 citation_faithfulness 列表相等。
    restored = PaperWorkspace.from_dict(ws.to_dict())
    assert restored.citation_faithfulness == expected_dicts

    # 且经 from_dict 重建为发现对象后与原列表逐条相等。
    rebuilt = [
        CitationFaithfulnessFinding.from_dict(d) for d in restored.citation_faithfulness
    ]
    assert rebuilt == findings

    # 向后兼容：缺失 citation_faithfulness 键的旧版 dict 回落空列表且不抛异常。
    legacy = ws.to_dict()
    del legacy["citation_faithfulness"]
    legacy_restored = PaperWorkspace.from_dict(legacy)
    assert legacy_restored.citation_faithfulness == []


# --------------------------------------------------------------------------- #
# Property 14: 再次运行替换而非累加
# --------------------------------------------------------------------------- #

# Feature: citation-faithfulness-audit, Property 14: 再次运行替换而非累加
@given(ws_first=_workspace(), ws_second=_workspace())
@settings(max_examples=100)
def test_prop14_rerun_replaces_not_accumulates(ws_first, ws_second):
    """Validates: Requirements 5.5, 9.5"""
    agent = _make_agent()

    result_first = agent.run(AgentContext(workspace=ws_first))
    result_second = agent.run(AgentContext(workspace=ws_second))

    # 仅第二次运行的报告（在独立工作区上单独应用得到）。
    scratch = _make_ws()
    _apply(scratch, result_second.mutations)
    report_second_only = list(scratch.citation_faithfulness)

    # 依次应用两次 mutation 到同一目标工作区。
    target = _make_ws()
    _apply(target, result_first.mutations)
    _apply(target, result_second.mutations)

    # 只反映最后一次结果（替换而非累加）。
    assert target.citation_faithfulness == report_second_only
    # 条数等于第二次抽取到的对总数（含未验证对），与第一次无关。
    assert len(target.citation_faithfulness) == _pair_total(ws_second)
