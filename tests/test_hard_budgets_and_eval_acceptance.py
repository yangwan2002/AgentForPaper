from __future__ import annotations

import json
import threading
import time

import pytest

from paper_agent.agents.base import AgentContext
from paper_agent.agents.citation_faithfulness_agent import (
    CitationFaithfulnessAgent,
    FaithfulnessJudge,
)
from paper_agent.evaluation.metrics import diagnostics, evaluate_assertions
from paper_agent.evaluation.models import EvalCase
from paper_agent.evaluation.runner import EvalRunner
from paper_agent.config import Config
from paper_agent.observability.budget import (
    BudgetExceededError,
    RunBudgetContext,
    activate_run_budget,
    reset_run_budget,
)
from paper_agent.observability.events import NullSink
from paper_agent.observability.llm_wrapper import ObservableLLMProvider
from paper_agent.observability.usage import UsageTracker
from paper_agent.orchestrator import PaperResult
from paper_agent.parsing.structured_parser import ParseOutcome
from paper_agent.providers.llm.base import (
    CancellationToken,
    LLMResponse,
    Message,
    StreamChunk,
)
from paper_agent.workspace.models import (
    InputMode,
    OutputFormat,
    PaperWorkspace,
    ParseStatus,
    ReferenceEntry,
    SectionDraft,
)


class _CountingLLM:
    def __init__(self) -> None:
        self.calls = 0

    def complete(self, messages, **opts):
        self.calls += 1
        return LLMResponse(content="ok", prompt_tokens=2, completion_tokens=1)


def test_observable_rejects_over_cap_before_provider_call():
    inner = _CountingLLM()
    tracker = UsageTracker()
    llm = ObservableLLMProvider(
        inner, NullSink(), tracker, token_cap=2, role="writer"
    )
    with pytest.raises(BudgetExceededError):
        llm.complete([Message("user", "a prompt that exceeds two tokens")])
    assert inner.calls == 0
    assert tracker.calls == 0


def test_usage_tracker_accounts_by_role():
    tracker = UsageTracker()
    writer = ObservableLLMProvider(
        _CountingLLM(), NullSink(), tracker, role="writer"
    )
    reviewer = ObservableLLMProvider(
        _CountingLLM(), NullSink(), tracker, role="reviewer"
    )
    writer.complete([Message("user", "write")])
    reviewer.complete([Message("user", "review")])
    assert tracker.calls == 2
    assert tracker.role_usage("writer").total_tokens == 3
    assert tracker.role_usage("reviewer").calls == 1


def test_run_budget_treats_exact_cap_as_exhausted():
    with pytest.raises(BudgetExceededError):
        RunBudgetContext(token_cap=3).check(total_tokens=3)


class _TimeoutIgnoringLLM:
    def __init__(self, delay: float = 0.2) -> None:
        self.delay = delay
        self.calls = 0
        self.seen_timeout: float | None = None

    def complete(self, messages, **opts):
        self.calls += 1
        self.seen_timeout = opts.get("timeout")
        time.sleep(self.delay)
        return LLMResponse(content="late", prompt_tokens=2, completion_tokens=1)


def test_complete_returns_at_deadline_when_provider_ignores_timeout():
    inner = _TimeoutIgnoringLLM()
    tracker = UsageTracker()
    llm = ObservableLLMProvider(inner, NullSink(), tracker)
    budget = RunBudgetContext(duration_cap_s=0.05)
    token = activate_run_budget(budget)
    started = time.monotonic()
    try:
        with pytest.raises(BudgetExceededError, match="deadline"):
            llm.complete([Message("user", "slow")], timeout=10)
    finally:
        reset_run_budget(token)

    assert time.monotonic() - started < 0.15
    assert inner.seen_timeout is not None
    assert 0 < inner.seen_timeout <= 0.05
    # Let the daemon finish and prove its late response is not accounted.
    time.sleep(0.2)
    assert tracker.calls == 0


class _BlockingStream:
    def __init__(self) -> None:
        self.closed = threading.Event()

    def __iter__(self):
        return self

    def __next__(self):
        time.sleep(0.2)
        return StreamChunk("content", "late")

    def close(self) -> None:
        self.closed.set()


class _TimeoutIgnoringStreamLLM:
    def __init__(self) -> None:
        self.stream_obj = _BlockingStream()
        self.token = None
        self.seen_timeout = None

    def stream(self, messages, *, cancel_token=None, **opts):
        self.token = cancel_token
        self.seen_timeout = opts.get("timeout")
        return self.stream_obj


def test_stream_deadline_cancels_combined_token_and_closes_inner_stream():
    inner = _TimeoutIgnoringStreamLLM()
    llm = ObservableLLMProvider(inner, NullSink())
    caller_token = CancellationToken()
    budget = RunBudgetContext(duration_cap_s=0.05)
    budget_token = activate_run_budget(budget)
    started = time.monotonic()
    try:
        with pytest.raises(BudgetExceededError, match="deadline"):
            list(
                llm.stream(
                    [Message("user", "slow")],
                    cancel_token=caller_token,
                    timeout=10,
                )
            )
    finally:
        reset_run_budget(budget_token)

    assert time.monotonic() - started < 0.15
    assert inner.token is not caller_token
    assert inner.token.cancelled
    assert inner.stream_obj.closed.wait(0.1)
    assert 0 < inner.seen_timeout <= 0.05


class _CountingParser:
    def __init__(self) -> None:
        self.calls = 0

    def request_json(self, messages, *, required_keys=()):
        self.calls += 1
        return ParseOutcome(
            status=ParseStatus.PARSED,
            data={"verdict": "supported", "rationale": "ok"},
        )


def _faithfulness_ws() -> PaperWorkspace:
    ws = PaperWorkspace(workspace_id="f", input_mode=InputMode.GENERATION)
    ws.verified_references = [
        ReferenceEntry(
            id="r1",
            title="Reference",
            authors=["A"],
            year=2024,
            source_id="r1",
            source="arxiv",
            verified=True,
            abstract="A sufficiently long grounding abstract for the claim.",
        )
    ]
    ws.section_drafts = {
        "s": SectionDraft(
            section_id="s",
            title="S",
            content="First supported claim [r1]. Second supported claim [r1].",
        )
    }
    return ws


def test_faithfulness_caps_claims_and_reuses_across_feedback_rounds():
    parser = _CountingParser()
    agent = CitationFaithfulnessAgent(
        FaithfulnessJudge(parser),
        min_grounding_chars=0,
        token_budget=500,
        max_claims=1,
        deadline_s=10,
    )
    ws = _faithfulness_ws()
    first = agent.run(AgentContext(workspace=ws))
    for mutation in first.mutations:
        mutation(ws)
    assert parser.calls == 1
    assert len(ws.citation_faithfulness) == 2
    assert any(
        item["rationale"] == "faithfulness_max_claims_reached"
        for item in ws.citation_faithfulness
    )

    second = agent.run(AgentContext(workspace=ws))
    for mutation in second.mutations:
        mutation(ws)
    # 已判定的声明命中跨反馈轮缓存；每轮至多再判一个新声明。
    assert parser.calls == 2
    third = agent.run(AgentContext(workspace=ws))
    for mutation in third.mutations:
        mutation(ws)
    assert parser.calls == 2


def test_evaluation_cost_and_independent_reviewer_assertions():
    ws = PaperWorkspace(workspace_id="e", input_mode=InputMode.GENERATION)
    result = PaperResult("e", "quality_met", [], None)
    observed = diagnostics(ws, result, duration_s=2.0)
    observed.update(
        total_tokens=100,
        llm_calls=4,
        independent_reviewer=True,
        ingest_rejected=False,
    )
    case = EvalCase(
        case_id="costs",
        description="",
        input={"topic_background": "x"},
        assertions={
            "max_total_tokens": 100,
            "max_duration_s": 2.0,
            "max_llm_calls": 4,
            "requires_independent_reviewer": True,
            "ingest_rejected": False,
        },
    )
    assert all(m.passed for m in evaluate_assertions(case, ws, result, observed))


def test_eval_ingest_rejected_and_config_snapshot(tmp_path):
    case = EvalCase(
        case_id="reject",
        description="",
        input={"draft_path": "missing.pdf"},
        assertions={"ingest_rejected": True},
        source_path=str(tmp_path / "case.json"),
    )
    runner = EvalRunner(
        Config(
            reviewer_llm_provider="openai",
            reviewer_llm_model="reviewer",
            total_token_budget=123,
            total_llm_call_budget=5,
        ),
        output_root=str(tmp_path / "results"),
    )
    run, summary_path = runner.run([case])
    assert run.cases[0].passed
    summary = json.loads(open(summary_path, encoding="utf-8").read())
    assert summary["config"]["total_token_budget"] == 123
    assert summary["config"]["total_llm_call_budget"] == 5
    assert summary["config"]["independent_reviewer"] is True
