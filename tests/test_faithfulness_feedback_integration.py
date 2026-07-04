"""集成测试：忠实性审计 unsupported 发现驱动反馈闭环（citation-faithfulness-audit 任务 9.4）。

验证目标（Requirements 6.1, 6.3）：
- 当工作区的 ``citation_faithfulness`` 含某个 ``section_id`` 的 ``unsupported`` 发现，
  且该 ``section_id`` 存在于 ``ws.section_drafts`` 时，运行编排器反馈闭环
  （``_feedback_loop`` → ``_build_edits`` → 派发 ``WritingAgent``）后，写作智能体应在
  某一轮通过 ``extras["gate_fixes"][section_id]`` 收到该无支撑发现（被并入定位式修订项）。
- 且在该轮「忠实性达标」条件不满足（``_faithfulness_ok(ws)`` 为 False），
  因而 ``quality_met`` 不因忠实性而成立（终止原因不是 ``quality_met``）。

采用真实 ``Orchestrator`` + 轻量假智能体（复用 tests/test_quality_gate_integration.py 的构造模式）：
- 一个 SPY 写作智能体，记录每轮收到的 ``extras``，并写入名为 ``sec1`` 的非空章节草稿；
- 一个 stub 忠实性审计智能体，其 ``run`` 返回单条 mutation，向 ``ws.citation_faithfulness``
  写入一个针对 ``sec1`` 的 ``unsupported`` 发现（真实经 ``AgentResult.mutations`` 单一写入路径）。
"""

from __future__ import annotations

from paper_agent.agents.base import AgentContext, AgentResult
from paper_agent.config import Config
from paper_agent.orchestrator import Orchestrator, PaperRequest
from paper_agent.workspace.models import (
    OutlineNode,
    PaperWorkspace,
    ReviewRecord,
    ScoringDimension,
    SectionDraft,
)
from paper_agent.workspace.repository import WorkspaceRepository
from paper_agent.workspace.store import InMemoryStore

SECTION_ID = "sec1"
REF_ID = "ref1"


class _Plan:
    name = "plan"

    def run(self, ctx: AgentContext) -> AgentResult:
        def mut(w: PaperWorkspace) -> None:
            w.outline = [OutlineNode(section_id=SECTION_ID, title="引言", order=0)]

        return AgentResult(mutations=[mut])


class _Search:
    name = "search"

    def run(self, ctx: AgentContext) -> AgentResult:
        return AgentResult()


class _SpyWriter:
    """记录每轮收到的 extras，并写入一个非空的 sec1 章节草稿。

    每轮写入相同内容，使停滞检测最终生效或达到迭代上限而终止（均非 quality_met）。
    """

    name = "writer"

    def __init__(self) -> None:
        self.received_extras: list[dict] = []

    def run(self, ctx: AgentContext) -> AgentResult:
        # 拷贝一份，避免后续对同一 dict 的复用影响断言。
        self.received_extras.append(dict(ctx.extras or {}))

        def mut(w: PaperWorkspace) -> None:
            w.section_drafts[SECTION_ID] = SectionDraft(
                section_id=SECTION_ID,
                title="引言",
                content=f"本文提出方法并取得显著效果 [{REF_ID}]。",
                cited_reference_ids=[REF_ID],
            )

        return AgentResult(mutations=[mut])


class _PerfectReviewer:
    """总是给满分（LLM 维度全达标，PARSED），使唯一的达标阻碍来自忠实性。"""

    name = "reviewer"

    def run(self, ctx: AgentContext) -> AgentResult:
        rec = ReviewRecord(
            iteration=ctx.workspace.iteration + 1,
            scores={d: 10.0 for d in ScoringDimension},
        )

        def mut(w: PaperWorkspace) -> None:
            w.review_records.append(rec)

        return AgentResult(mutations=[mut])


class _StubFaithfulness:
    """stub 忠实性审计：写入针对 sec1 的单条 unsupported 发现（替换写入）。"""

    name = "faithfulness"

    def run(self, ctx: AgentContext) -> AgentResult:
        finding = {
            "section_id": SECTION_ID,
            "cited_reference_id": REF_ID,
            "claim_excerpt": f"本文提出方法并取得显著效果 [{REF_ID}]。",
            "verdict": "unsupported",
            "severity": "high",
            "rationale": "被引文献未提供支撑该声明的证据。",
            "supporting_snippet": "",
            "parse_status": "parsed",
            "unverified_reference": False,
        }

        def mut(w: PaperWorkspace) -> None:
            w.citation_faithfulness = [finding]

        return AgentResult(mutations=[mut])


def _build_orchestrator(tmp_path, *, faithfulness):
    repo = WorkspaceRepository(InMemoryStore())
    writer = _SpyWriter()
    config = Config(
        quality_threshold=8.0, iteration_limit=2, workspace_dir=str(tmp_path)
    )
    orch = Orchestrator(
        repo=repo,
        plan_agent=_Plan(),
        search_agent=_Search(),
        writing_agent=writer,
        review_agent=_PerfectReviewer(),
        config=config,
        faithfulness_agent=faithfulness,
    )
    return orch, repo, writer


def test_unsupported_finding_reaches_writer_as_gate_fix(tmp_path):
    """unsupported 发现应经 gate_fixes[section_id] 到达写作智能体，且该轮忠实性不达标。"""
    orch, repo, writer = _build_orchestrator(tmp_path, faithfulness=_StubFaithfulness())

    result = orch.run(PaperRequest(topic_background="主题"))
    ws = repo.load(result.workspace_id)

    # 1) 忠实性审计确实写入了 unsupported 发现（单一写入路径生效）。
    assert any(
        f.get("verdict") == "unsupported" and f.get("section_id") == SECTION_ID
        for f in ws.citation_faithfulness
    )

    # 2) 写作智能体在某一轮通过 extras["gate_fixes"][sec1] 收到该无支撑发现。
    gate_fix_rounds = [
        extras
        for extras in writer.received_extras
        if SECTION_ID in (extras.get("gate_fixes") or {})
    ]
    assert gate_fix_rounds, (
        "写作智能体从未收到 gate_fixes[sec1]；received_extras="
        f"{writer.received_extras}"
    )
    merged_msgs = gate_fix_rounds[-1]["gate_fixes"][SECTION_ID]
    # 合并进来的定位式修订项应引用无支撑忠实性信号（含引用 id）。
    assert any("忠实性" in m and REF_ID in m for m in merged_msgs), merged_msgs

    # 3) 该轮忠实性达标条件不满足 → quality_met 不因忠实性成立。
    assert orch._faithfulness_ok(ws) is False
    assert result.terminated_reason != "quality_met"


def test_no_unsupported_means_faithfulness_ok(tmp_path):
    """对照：无 unsupported 发现时 _faithfulness_ok 为真，且写作不会收到忠实性 gate_fix。

    证明上一个用例中的阻断确实由 unsupported 发现驱动（区分性）。
    """

    class _CleanFaithfulness:
        name = "faithfulness"

        def run(self, ctx: AgentContext) -> AgentResult:
            def mut(w: PaperWorkspace) -> None:
                # 只写一个 supported 发现——不应触发 gate_fix，也不应阻断达标。
                w.citation_faithfulness = [
                    {
                        "section_id": SECTION_ID,
                        "cited_reference_id": REF_ID,
                        "verdict": "supported",
                        "severity": "none",
                        "rationale": "",
                        "claim_excerpt": "",
                        "supporting_snippet": "",
                        "parse_status": "parsed",
                        "unverified_reference": False,
                    }
                ]

            return AgentResult(mutations=[mut])

    orch, repo, writer = _build_orchestrator(tmp_path, faithfulness=_CleanFaithfulness())
    result = orch.run(PaperRequest(topic_background="主题"))
    ws = repo.load(result.workspace_id)

    assert orch._faithfulness_ok(ws) is True
    # 写作智能体不应收到「忠实性·无支撑」定位式修订项（质量闸可能因未验证引用另行
    # 产生 gate_fix，但那不是忠实性信号）。
    faithfulness_msgs = [
        m
        for extras in writer.received_extras
        for m in (extras.get("gate_fixes") or {}).get(SECTION_ID, [])
        if "忠实性" in m
    ]
    assert not faithfulness_msgs, faithfulness_msgs
