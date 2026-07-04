"""单元测试：判定器 rationale / supporting_snippet 透传与严重度映射（Req 4.6）。

任务 7.10：用桩 parser 返回 PARSED（带具体 rationale / supporting_snippet 与
verdict），在一个「单条已验证对且 grounding 充足」的工作区上运行
``CitationFaithfulnessAgent.run``，断言写回 ``ws.citation_faithfulness`` 的发现：

- 逐字透传 ``rationale`` 与 ``supporting_snippet``；
- ``unsupported`` → ``severity == "high"``、``weak_support`` → ``severity == "medium"``；
- ``verdict`` 正确落盘。

均为普通 pytest 用例，判定器经注入桩 parser 提供，不改动生产代码。
"""

from __future__ import annotations

from paper_agent.agents.base import AgentContext
from paper_agent.agents.citation_faithfulness_agent import (
    CitationFaithfulnessAgent,
    FaithfulnessJudge,
)
from paper_agent.parsing.structured_parser import ParseOutcome
from paper_agent.workspace.faithfulness import FaithfulnessVerdict
from paper_agent.workspace.models import (
    InputMode,
    OutputFormat,
    PaperWorkspace,
    ParseStatus,
    ReferenceEntry,
    SectionDraft,
)

# 判定器透传的哨兵值：既不出现在 reference_meta（title/year/authors），也不会
# 由被测编排合成——若在发现中原样出现，即证明来自 parser 的 PARSED data。
_RATIONALE = "该声明句与摘要的实验结论不一致（rationale sentinel 42）"
_SNIPPET = "supporting snippet sentinel: 'we observe no such effect'"

# 被引用的已验证文献 id 与引用它的单一章节。
_REF_ID = "ref1"


class _StubParser:
    """桩 StructuredParser：始终返回 PARSED，携带指定 verdict/rationale/snippet。

    与 ``FaithfulnessJudge`` 的调用签名一致：``request_json(messages, *, required_keys)``。
    """

    def __init__(self, verdict: str) -> None:
        self._verdict = verdict

    def request_json(self, messages, *, required_keys=()) -> ParseOutcome:  # noqa: D401
        return ParseOutcome(
            status=ParseStatus.PARSED,
            data={
                "verdict": self._verdict,
                "rationale": _RATIONALE,
                "supporting_snippet": _SNIPPET,
            },
        )


def _make_ws() -> PaperWorkspace:
    """构造单条已验证文献（title/abstract 非空 → grounding 充足）+ 单章节引用它。"""
    ws = PaperWorkspace(
        workspace_id="ws-unit",
        input_mode=InputMode.GENERATION,
        output_format=OutputFormat.LATEX,
        topic_background="多智能体协作写作",
    )
    ws.verified_references = [
        ReferenceEntry(
            id=_REF_ID,
            title="Faithful Citation Auditing",
            authors=["A. Author"],
            year=2020,
            source_id=_REF_ID,
            source="arxiv",
            verified=True,
            abstract="A non-empty abstract that provides sufficient grounding text.",
        )
    ]
    ws.section_drafts = {
        "s0": SectionDraft(
            section_id="s0",
            title="Intro",
            content=f"这是一个待核验的声明句 [{_REF_ID}]。",
        )
    }
    return ws


def _run_and_get_finding(verdict: str) -> dict:
    """在充足 grounding 工作区上以指定 verdict 运行 agent，返回唯一那条发现。"""
    # min_grounding_chars=0：不因 grounding 长度短路，保证判定器被调用。
    agent = CitationFaithfulnessAgent(
        FaithfulnessJudge(_StubParser(verdict)),
        min_grounding_chars=0,
        token_budget=200,
    )
    ws = _make_ws()
    result = agent.run(AgentContext(workspace=ws))
    for mut in result.mutations:
        mut(ws)

    report = ws.citation_faithfulness
    assert len(report) == 1, f"预期恰好一条发现，实得 {len(report)}：{report}"
    finding = report[0]
    # 前置健全性：确为已验证对的发现（非 unverified 短路路径）。
    assert finding["cited_reference_id"] == _REF_ID
    assert finding["unverified_reference"] is False
    assert finding["parse_status"] == ParseStatus.PARSED.value
    return finding


def test_unsupported_passthrough_rationale_snippet_and_high_severity():
    """unsupported 发现：severity=high，且逐字透传 rationale / supporting_snippet。"""
    finding = _run_and_get_finding("unsupported")

    assert finding["verdict"] == FaithfulnessVerdict.UNSUPPORTED.value
    assert finding["severity"] == "high"
    assert finding["rationale"] == _RATIONALE
    assert finding["supporting_snippet"] == _SNIPPET


def test_weak_support_passthrough_rationale_snippet_and_medium_severity():
    """weak_support 发现：severity=medium，且逐字透传 rationale / supporting_snippet。"""
    finding = _run_and_get_finding("weak_support")

    assert finding["verdict"] == FaithfulnessVerdict.WEAK_SUPPORT.value
    assert finding["severity"] == "medium"
    assert finding["rationale"] == _RATIONALE
    assert finding["supporting_snippet"] == _SNIPPET
