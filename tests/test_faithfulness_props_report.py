"""Property-based tests for citation-faithfulness-audit 报告完备性与写入路径。

- Property 11: 报告与对一一对应且字段完备（Req 5.1）。
- Property 12: 单一写入路径（Req 5.2 / 9.1）。

生成器约束：字符串一律排除 unicode 代理区与控制字符（categories "Cs" / "Cc"），
避免代理/控制字符导致的序列化/处理异常。
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
# 引用 id 仅取 ASCII 标识符字符子集（与抽取器 _TEXT_CITATION 认可的字符一致）。
_ID_TEXT = st.text(
    alphabet="abcdefghijklmnopqrstuvwxyz0123456789", min_size=1, max_size=6
)

# 报告 finding 的必备字段集合（Req 5.1）。
_REQUIRED_KEYS = {
    "section_id",
    "cited_reference_id",
    "claim_excerpt",
    "verdict",
    "severity",
    "parse_status",
}


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
# Property 11: 报告与对一一对应且字段完备
# --------------------------------------------------------------------------- #

# Feature: citation-faithfulness-audit, Property 11: 报告与对一一对应且字段完备
@given(ws=_workspace())
@settings(max_examples=100)
def test_prop11_report_one_to_one_with_pairs_and_complete_fields(ws):
    """Validates: Requirements 5.1"""
    agent = _make_agent()

    result = agent.run(AgentContext(workspace=ws))
    _apply(ws, result.mutations)

    findings = ws.citation_faithfulness

    # 报告条数 == 抽取对总数（已验证 + 未验证），一一对应。
    assert len(findings) == _pair_total(ws)

    # 每条 finding 均含必备字段（section_id / cited_reference_id / claim_excerpt /
    # verdict / severity / parse_status）。
    for finding in findings:
        missing = _REQUIRED_KEYS - set(finding.keys())
        assert not missing, f"finding 缺失字段: {missing}; finding={finding}"


# --------------------------------------------------------------------------- #
# Property 12: 单一写入路径
# --------------------------------------------------------------------------- #

# Feature: citation-faithfulness-audit, Property 12: 单一写入路径
@given(ws=_workspace())
@settings(max_examples=100)
def test_prop12_single_write_path(ws):
    """Validates: Requirements 5.2, 9.1"""
    agent = _make_agent()

    before = list(ws.citation_faithfulness)  # 记录运行前的值（新工作区应为 []）。

    result = agent.run(AgentContext(workspace=ws))

    # run 恰好返回一条 mutation（单一写入路径）。
    assert len(result.mutations) == 1

    # run 本身不改动传入工作区的 citation_faithfulness —— 写入只发生在应用 mutation 时。
    assert ws.citation_faithfulness == before

    # 应用唯一的 mutation 后，报告被写入（条数等于抽取对总数）。
    _apply(ws, result.mutations)
    assert len(ws.citation_faithfulness) == _pair_total(ws)
