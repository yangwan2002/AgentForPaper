from paper_agent.agents.base import AgentContext
from paper_agent.agents.citation_faithfulness_agent import (
    CitationFaithfulnessAgent,
    FaithfulnessJudge,
)
from paper_agent.parsing.structured_parser import ParseOutcome
from paper_agent.workspace.models import (
    InputMode,
    PaperWorkspace,
    ParseStatus,
    ReferenceEntry,
    SectionDraft,
)


class _BatchParser:
    def __init__(self) -> None:
        self.calls = 0

    def request_json(self, messages, required_keys=()):
        self.calls += 1
        assert "批量输入" in messages[-1].content
        return ParseOutcome(
            status=ParseStatus.PARSED,
            data={
                "results": [
                    {
                        "id": str(index),
                        "verdict": "supported",
                        "rationale": "grounded",
                        "supporting_snippet": "evidence",
                    }
                    for index in range(8)
                ]
            },
        )


def test_eight_claims_use_one_batch_call_and_cache_across_runs() -> None:
    parser = _BatchParser()
    agent = CitationFaithfulnessAgent(
        FaithfulnessJudge(parser),
        min_grounding_chars=1,
        token_budget=1000,
        max_claims=16,
    )
    ws = PaperWorkspace(workspace_id="batch", input_mode=InputMode.DRAFT_REVISION)
    ws.verified_references = [
        ReferenceEntry(
            id="r1",
            title="Reference",
            authors=["A"],
            year=2024,
            source_id="doi:1",
            verified=True,
            abstract="evidence " * 100,
        )
    ]
    ws.section_drafts["s"] = SectionDraft(
        section_id="s",
        title="Section",
        content=" ".join(
            f"Claim number {index} is supported [r1]." for index in range(8)
        ),
    )

    for mutation in agent.run(AgentContext(workspace=ws)).mutations:
        mutation(ws)
    assert parser.calls == 1
    assert len(ws.citation_faithfulness) == 8

    for mutation in agent.run(AgentContext(workspace=ws)).mutations:
        mutation(ws)
    assert parser.calls == 1


def test_max_claims_budget_prefers_high_priority_claims() -> None:
    from paper_agent.workspace.faithfulness import FaithfulnessVerdict

    class _VerdictParser:
        def __init__(self) -> None:
            self.judged_claims: list[str] = []

        def request_json(self, messages, required_keys=()):
            if required_keys == ("results",):
                return ParseOutcome(status=ParseStatus.PARSED, data={"results": []})
            self.judged_claims.append(messages[-1].content)
            return ParseOutcome(
                status=ParseStatus.PARSED,
                data={
                    "verdict": "supported",
                    "rationale": "ok",
                    "supporting_snippet": "evidence",
                },
            )

        def judge_batch(self, items):
            self.judged_claims.extend(item["claim"] for item in items)
            return [
                (FaithfulnessVerdict.SUPPORTED, "ok", "evidence", ParseStatus.PARSED)
                for _ in items
            ]

    parser = _VerdictParser()
    agent = CitationFaithfulnessAgent(
        FaithfulnessJudge(parser),
        min_grounding_chars=1,
        token_budget=1000,
        max_claims=2,
    )
    ws = PaperWorkspace(workspace_id="prio", input_mode=InputMode.DRAFT_REVISION)
    ws.verified_references = [
        ReferenceEntry(
            id="r1",
            title="Reference",
            authors=["A"],
            year=2024,
            source_id="doi:1",
            verified=True,
            abstract="evidence " * 100,
        )
    ]
    ws.section_drafts["s"] = SectionDraft(
        section_id="s",
        title="Section",
        content=(
            "相关研究已有较多讨论 [r1]。"
            "实验误差降低至 0.052 m [r1]。"
            "SLAM 方法在 AprilTag 上表现更好 [r1]。"
        ),
    )

    for mutation in agent.run(AgentContext(workspace=ws)).mutations:
        mutation(ws)

    capped = [
        item
        for item in ws.citation_faithfulness
        if item.get("rationale") == "faithfulness_max_claims_reached"
    ]
    assert len(capped) == 1
    assert "较多讨论" in capped[0].get("claim_excerpt", "")
