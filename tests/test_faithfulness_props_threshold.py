"""Property-based test for citation-faithfulness-audit 阈值可配置且被采用。

- Property 19: 阈值可配置且被采用（Req 8.3）。

两个可配置阈值必须被判定管线真正采用：

(a) ``min_grounding_chars`` 是 grounding 充足性短路的边界：构造一个已验证文献，
    其组装后的 grounding 有已知长度 ``L``。当 ``min_grounding_chars <= L`` 时该对
    可进入判定器（judge 被调用，裁决可非 cannot_verify）；当
    ``min_grounding_chars > L`` 时被强制 cannot_verify 且**不**调用 judge。断言翻转
    恰好发生在 ``L`` 与 ``L + 1`` 的边界。

(b) ``token_budget`` 决定 grounding 文本截断长度：``len(assemble_grounding(ref, b))``
    恒 ``<= b``；对来源长于 ``b`` 的文献恒 ``== b``。两个不同预算产生相应受界长度。

生成器约束：排除 unicode 代理区 "Cs" 与控制字符 "Cc"，长度受限。
"""

from __future__ import annotations

from hypothesis import given, settings
from hypothesis import strategies as st

from paper_agent.agents.base import AgentContext
from paper_agent.agents.citation_faithfulness_agent import CitationFaithfulnessAgent
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
_ID_TEXT = st.text(
    alphabet="abcdefghijklmnopqrstuvwxyz0123456789", min_size=1, max_size=6
)

# 远大于任何生成内容长度的预算：用于测得未截断的自然长度 L / F。
_HUGE_BUDGET = 10**9


class _SpyJudge:
    """间谍判定器：记录调用次数，恒返回 supported（PARSED）。

    暴露与 ``FaithfulnessJudge`` 同形的 ``judge(...)`` 方法，供被测智能体经依赖
    注入调用。返回 supported 便于区分「被判定」（非 cannot_verify）与「被短路」
    （cannot_verify 且 0 次调用）两条路径。
    """

    def __init__(self) -> None:
        self.calls = 0

    def judge(self, *, claim, grounding, reference_meta):  # noqa: D401, ANN001
        self.calls += 1
        return (FaithfulnessVerdict.SUPPORTED, "ok", "snip", ParseStatus.PARSED)


def _make_ws() -> PaperWorkspace:
    return PaperWorkspace(
        workspace_id="ws1",
        input_mode=InputMode.GENERATION,
        output_format=OutputFormat.LATEX,
        topic_background="阈值可配置性验证",
    )


def _ref(rid: str, *, title: str, abstract: str) -> ReferenceEntry:
    """构造一条已验证文献（verified=True，具 title/abstract 供 grounding 组装）。"""
    return ReferenceEntry(
        id=rid,
        title=title,
        authors=["A. Author"],
        year=2020,
        source_id=rid,
        source="arxiv",
        verified=True,
        abstract=abstract,
    )


def _ws_with_single_verified_pair(ref: ReferenceEntry) -> PaperWorkspace:
    """构造仅含单个已验证声明-引用对的最小工作区。

    章节正文仅内嵌一个 ``[rid]`` 标注（rid 为 ASCII 标识符，匹配引用扫描正则），
    从而 ``extract_pairs`` 恰好产出一个已验证对。
    """
    ws = _make_ws()
    ws.verified_references = [ref]
    ws.section_drafts = {
        "s0": SectionDraft(
            section_id="s0",
            title="Intro",
            content=f"这是一个声明句 [{ref.id}]。",
        )
    }
    return ws


def _single_finding(ws: PaperWorkspace, result) -> dict:  # noqa: ANN001
    """应用 result 的 mutation 到独立工作区并返回唯一一条发现。"""
    scratch = _make_ws()
    for mut in result.mutations:
        mut(scratch)
    findings = scratch.citation_faithfulness
    assert len(findings) == 1, f"预期恰好 1 条发现，实得 {len(findings)}"
    return findings[0]


# --------------------------------------------------------------------------- #
# Property 19: 阈值可配置且被采用
# --------------------------------------------------------------------------- #

# Feature: citation-faithfulness-audit, Property 19: 阈值可配置且被采用
@given(
    rid=_ID_TEXT,
    title_seed=_SAFE_TEXT,
    abstract_seed=_SAFE_TEXT,
    budget_a=st.integers(min_value=0, max_value=120),
    budget_b=st.integers(min_value=0, max_value=120),
)
@settings(max_examples=100)
def test_prop19_thresholds_are_configurable_and_adopted(
    rid, title_seed, abstract_seed, budget_a, budget_b
):
    """Validates: Requirements 8.3"""
    # 前缀保证 title 去空白后非空 ⇒ 组装 grounding 非空、L >= 1。
    ref = _ref(rid, title="T" + title_seed, abstract=abstract_seed)

    # ---------------------------------------------------------------------- #
    # (a) min_grounding_chars 边界：恰在 L 与 L+1 之间翻转充足性短路
    # ---------------------------------------------------------------------- #
    # 用超大预算测得未截断的自然 grounding 长度 L。
    L = len(assemble_grounding(ref, token_budget=_HUGE_BUDGET))
    assert L >= 1  # 前缀 "T" 保证非空

    ws = _ws_with_single_verified_pair(ref)

    # min_grounding_chars = L：len(grounding)=L 不小于阈值 ⇒ 可判定（judge 被调用）。
    # 智能体 token_budget 取超大值，确保其内部组装长度等于 L（不被截断）。
    spy_allow = _SpyJudge()
    agent_allow = CitationFaithfulnessAgent(
        spy_allow, min_grounding_chars=L, token_budget=_HUGE_BUDGET
    )
    result_allow = agent_allow.run(AgentContext(workspace=ws))
    finding_allow = _single_finding(ws, result_allow)

    assert spy_allow.calls == 1, "min_grounding_chars == L 时应调用判定器"
    assert finding_allow["verdict"] != FaithfulnessVerdict.CANNOT_VERIFY.value, (
        "min_grounding_chars == L 时不应被强制短路为 cannot_verify"
    )

    # min_grounding_chars = L + 1：len(grounding)=L 严格小于阈值 ⇒ 强制 cannot_verify，
    # 且**不**调用判定器。
    spy_block = _SpyJudge()
    agent_block = CitationFaithfulnessAgent(
        spy_block, min_grounding_chars=L + 1, token_budget=_HUGE_BUDGET
    )
    result_block = agent_block.run(AgentContext(workspace=ws))
    finding_block = _single_finding(ws, result_block)

    assert spy_block.calls == 0, "min_grounding_chars == L+1 时不应调用判定器"
    assert finding_block["verdict"] == FaithfulnessVerdict.CANNOT_VERIFY.value, (
        "min_grounding_chars == L+1 时应被短路为 cannot_verify"
    )

    # ---------------------------------------------------------------------- #
    # (b) token_budget 决定截断长度：len(assemble_grounding(ref, b)) == min(b, F)
    # ---------------------------------------------------------------------- #
    F = len(assemble_grounding(ref, token_budget=_HUGE_BUDGET))  # 自然完整长度

    for b in (budget_a, budget_b):
        length = len(assemble_grounding(ref, token_budget=b))
        # 恒不超过预算上限。
        assert length <= b
        # 来源长于 b（F > b）时恰等于 b；否则等于自然长度 F。二者即 min(b, F)。
        assert length == min(b, F)

    # 两个不同预算产生相应受界长度：较小预算不产更长文本（单调不减）。
    len_a = len(assemble_grounding(ref, token_budget=budget_a))
    len_b = len(assemble_grounding(ref, token_budget=budget_b))
    if budget_a <= budget_b:
        assert len_a <= len_b
    else:
        assert len_b <= len_a
