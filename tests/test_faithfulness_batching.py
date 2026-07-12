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
