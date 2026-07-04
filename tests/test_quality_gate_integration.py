"""质量闸与反馈循环的集成：高分但有高严重度问题时不应判为达标。"""

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


class _Plan:
    name = "plan"

    def run(self, ctx: AgentContext) -> AgentResult:
        def mut(w: PaperWorkspace) -> None:
            w.outline = [OutlineNode(section_id="intro", title="引言", order=0)]
        return AgentResult(mutations=[mut])


class _Search:
    name = "search"

    def run(self, ctx: AgentContext) -> AgentResult:
        return AgentResult()


class _EmptyWriter:
    """总是把章节写成空内容（触发质量闸 empty_section 高严重度）。"""

    name = "writer"

    def run(self, ctx: AgentContext) -> AgentResult:
        def mut(w: PaperWorkspace) -> None:
            w.section_drafts["intro"] = SectionDraft(
                section_id="intro", title="引言", content=""
            )
        return AgentResult(mutations=[mut])


class _PerfectReviewer:
    """总是给满分（LLM 维度全达标）。"""

    name = "reviewer"

    def run(self, ctx: AgentContext) -> AgentResult:
        rec = ReviewRecord(
            iteration=ctx.workspace.iteration + 1,
            scores={d: 10.0 for d in ScoringDimension},
        )

        def mut(w: PaperWorkspace) -> None:
            w.review_records.append(rec)
        return AgentResult(mutations=[mut])


def test_gate_blocks_quality_met_despite_perfect_scores(tmp_path):
    repo = WorkspaceRepository(InMemoryStore())
    config = Config(quality_threshold=8.0, iteration_limit=2, workspace_dir=str(tmp_path))
    orch = Orchestrator(
        repo=repo,
        plan_agent=_Plan(),
        search_agent=_Search(),
        writing_agent=_EmptyWriter(),
        review_agent=_PerfectReviewer(),
        config=config,
    )
    result = orch.run(PaperRequest(topic_background="主题"))

    # LLM 满分，但空章节是高严重度问题 → 不能判为达标，最终因迭代上限终止。
    assert result.terminated_reason == "iteration_limit"
    ws = repo.load(result.workspace_id)
    assert any(i["type"] == "empty_section" for i in ws.quality_report)
