from __future__ import annotations

import paper_agent.agents.citation_faithfulness_agent as faithfulness_module
from paper_agent.agents.base import AgentContext
from paper_agent.agents.citation_faithfulness_agent import (
    CitationFaithfulnessAgent,
    FaithfulnessJudge,
)
from paper_agent.observability.budget import BudgetExceededError
from paper_agent.parsing.structured_parser import ParseOutcome
from paper_agent.workspace.models import (
    InputMode,
    PaperWorkspace,
    ParseStatus,
    ReferenceEntry,
    SectionDraft,
)


def _workspace(count: int = 8) -> PaperWorkspace:
    ws = PaperWorkspace(workspace_id="deep", input_mode=InputMode.GENERATION)
    ws.verified_references = [
        ReferenceEntry(
            id="r1",
            title="Reference",
            authors=["A"],
            year=2024,
            source_id="doi:1",
            verified=True,
            abstract="direct grounding evidence " * 50,
        )
    ]
    ws.section_drafts["s"] = SectionDraft(
        section_id="s",
        title="Section",
        content=" ".join(
            f"Distinct claim number {index} is stated [r1]."
            for index in range(count)
        ),
    )
    return ws


def _apply(agent: CitationFaithfulnessAgent, ws: PaperWorkspace) -> list[dict]:
    result = agent.run(AgentContext(workspace=ws))
    for mutation in result.mutations:
        mutation(ws)
    return ws.citation_faithfulness


class _RoutingParser:
    def __init__(
        self,
        batch_verdicts: list[str],
        *,
        deep_verdict: str = "supported",
        fail_first_deep: bool = False,
        clock: dict[str, float] | None = None,
    ) -> None:
        self.batch_verdicts = batch_verdicts
        self.deep_verdict = deep_verdict
        self.fail_first_deep = fail_first_deep
        self.clock = clock
        self.batch_calls = 0
        self.deep_calls = 0

    def request_json(self, messages, *, required_keys=()):
        if "批量输入" in messages[-1].content:
            self.batch_calls += 1
            if self.clock is not None:
                self.clock["now"] = 2.0
            return ParseOutcome(
                status=ParseStatus.PARSED,
                data={
                    "results": [
                        {
                            "id": str(index),
                            "verdict": verdict,
                            "rationale": f"batch-{verdict}",
                            "supporting_snippet": "batch evidence",
                        }
                        for index, verdict in enumerate(self.batch_verdicts)
                    ]
                },
            )
        if "严格复核员" in messages[0].content:
            self.deep_calls += 1
            if self.fail_first_deep and self.deep_calls == 1:
                raise BudgetExceededError("llm_calls", limit=1, observed=1)
            return ParseOutcome(
                status=ParseStatus.PARSED,
                data={
                    "verdict": self.deep_verdict,
                    "rationale": "deep-grounded",
                    "supporting_snippet": "direct evidence",
                },
            )
        raise AssertionError("unexpected per-item first-level call")


def test_only_weak_batch_results_receive_one_deep_review() -> None:
    parser = _RoutingParser(
        [
            "supported",
            "unsupported",
            "weak_support",
            "cannot_verify",
            "supported",
            "unsupported",
            "supported",
            "cannot_verify",
        ]
    )
    agent = CitationFaithfulnessAgent(
        FaithfulnessJudge(parser),
        min_grounding_chars=1,
        token_budget=1000,
        max_claims=8,
    )

    report = _apply(agent, _workspace())

    assert parser.batch_calls == 1
    assert parser.deep_calls == 1
    assert sum(item["rationale"] == "deep-grounded" for item in report) == 1
    assert sum(item["rationale"] == "batch-supported" for item in report) == 3
    assert sum(item["rationale"] == "batch-unsupported" for item in report) == 2


def test_deep_review_cache_hashes_claim_reference_and_grounding() -> None:
    parser = _RoutingParser(
        ["weak_support", *["supported"] * 7]
    )
    agent = CitationFaithfulnessAgent(
        FaithfulnessJudge(parser),
        min_grounding_chars=1,
        token_budget=1000,
        max_claims=8,
    )
    ws = _workspace()

    _apply(agent, ws)
    assert (parser.batch_calls, parser.deep_calls) == (1, 1)

    agent._cache.clear()
    _apply(agent, ws)
    assert (parser.batch_calls, parser.deep_calls) == (2, 1)

    ws.verified_references[0].abstract = (
        "changed grounding " + ws.verified_references[0].abstract
    )
    agent._cache.clear()
    _apply(agent, ws)
    assert (parser.batch_calls, parser.deep_calls) == (3, 2)


def test_deadline_prevents_weak_deep_review(monkeypatch) -> None:
    clock = {"now": 0.0}
    monkeypatch.setattr(
        faithfulness_module.time, "monotonic", lambda: clock["now"]
    )
    parser = _RoutingParser(
        ["weak_support", *["supported"] * 7], clock=clock
    )
    agent = CitationFaithfulnessAgent(
        FaithfulnessJudge(parser),
        min_grounding_chars=1,
        token_budget=1000,
        max_claims=8,
        deadline_s=1.0,
    )

    report = _apply(agent, _workspace())

    assert parser.batch_calls == 1
    assert parser.deep_calls == 0
    assert any(
        item["rationale"] == "faithfulness_deadline_reached_before_deep_review"
        for item in report
    )


def test_deep_review_budget_error_isolated_and_fails_closed() -> None:
    parser = _RoutingParser(
        ["weak_support", "weak_support", *["supported"] * 6],
        fail_first_deep=True,
    )
    agent = CitationFaithfulnessAgent(
        FaithfulnessJudge(parser),
        min_grounding_chars=1,
        token_budget=1000,
        max_claims=8,
    )

    report = _apply(agent, _workspace())

    assert parser.batch_calls == 1
    assert parser.deep_calls == 2
    assert sum(item["verdict"] == "cannot_verify" for item in report) == 1
    assert sum(item["rationale"] == "deep-grounded" for item in report) == 1


class _CountingJudge:
    def __init__(self) -> None:
        self.batch_calls = 0
        self.deep_calls = 0

    def judge_batch(self, items):
        self.batch_calls += 1
        return [
            (
                faithfulness_module.FaithfulnessVerdict.WEAK_SUPPORT,
                "weak",
                "",
                ParseStatus.PARSED,
            )
            for _ in items
        ]

    def deep_review(self, *, claim, grounding, reference_meta):
        self.deep_calls += 1
        return (
            faithfulness_module.FaithfulnessVerdict.SUPPORTED,
            "deep",
            "evidence",
            ParseStatus.PARSED,
        )


def test_max_claims_also_bounds_deep_review_candidates() -> None:
    judge = _CountingJudge()
    agent = CitationFaithfulnessAgent(
        judge,
        min_grounding_chars=1,
        token_budget=1000,
        max_claims=8,
    )

    report = _apply(agent, _workspace(count=12))

    assert judge.batch_calls == 1
    assert judge.deep_calls == 8
    assert sum(
        item["rationale"] == "faithfulness_max_claims_reached"
        for item in report
    ) == 4
