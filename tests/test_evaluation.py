from __future__ import annotations

import json

import pytest

from paper_agent.config import Config
from paper_agent.evaluation.metrics import diagnostics, evaluate_assertions
from paper_agent.evaluation.models import EvalCase
from paper_agent.evaluation.runner import EvalRunner, discover_cases
from paper_agent.export.base import ExportResult
from paper_agent.ingestion import IngestionConfirmationRequired
from paper_agent.orchestrator import PaperResult
from paper_agent.workspace.models import (
    InputMode,
    OutputFormat,
    PaperWorkspace,
)


def test_discover_cases_loads_json_and_rejects_duplicate_ids(tmp_path):
    case = {
        "id": "case-1",
        "input": {"topic_background": "test"},
        "assertions": {"run_completed": True},
    }
    (tmp_path / "one.json").write_text(json.dumps(case), encoding="utf-8")
    loaded = discover_cases(str(tmp_path))
    assert [item.case_id for item in loaded] == ["case-1"]
    assert loaded[0].source_path.endswith("one.json")

    (tmp_path / "two.json").write_text(json.dumps(case), encoding="utf-8")
    try:
        discover_cases(str(tmp_path))
    except ValueError as exc:
        assert "重复评测案例 id" in str(exc)
    else:
        raise AssertionError("重复 id 应被拒绝")


def test_metrics_expose_hard_failures_and_unknown_assertions():
    ws = PaperWorkspace(
        workspace_id="w1",
        input_mode=InputMode.GENERATION,
        quality_report=[
            {
                "type": "fabricated_metric",
                "severity": "high",
                "section_id": "results",
            },
            {
                "type": "text_citation_invalid",
                "severity": "high",
                "section_id": "intro",
            },
        ],
        citation_faithfulness=[
            {"verdict": "unsupported"},
            {"verdict": "cannot_verify"},
        ],
    )
    result = PaperResult(
        workspace_id="w1",
        terminated_reason="iteration_limit",
        unmet_dimensions=[],
        export=ExportResult(OutputFormat.MARKDOWN, files=["paper.md"]),
        submittable=False,
    )
    observed = diagnostics(ws, result, duration_s=1.25)
    assert observed["fabricated_metrics"] == 1
    assert observed["fabricated_citations"] == 1
    assert observed["unsupported_citations"] == 1

    case = EvalCase(
        case_id="metrics",
        description="",
        input={"topic_background": "test"},
        assertions={
            "max_fabricated_metrics": 0,
            "max_unsupported_citations": 1,
            "misspelled_assertion": True,
        },
    )
    metrics = evaluate_assertions(case, ws, result, observed)
    by_name = {metric.name: metric for metric in metrics}
    assert by_name["max_fabricated_metrics"].passed is False
    assert by_name["max_unsupported_citations"].passed is True
    assert by_name["misspelled_assertion"].passed is False
    assert "未知评测断言" in by_name["misspelled_assertion"].details


def test_eval_runner_executes_mock_case_and_writes_report(tmp_path):
    case = EvalCase(
        case_id="runner-smoke",
        description="",
        input={
            "topic_background": "paper agent evaluation",
            "output_format": "markdown",
        },
        assertions={
            "run_completed": True,
            "export_created": True,
            "expected_format": "markdown",
        },
    )
    config = Config(
        llm_provider="mock",
        retrieval_provider="mock",
        iteration_limit=1,
        citation_faithfulness_enabled=True,
    )
    run, summary_path = EvalRunner(
        config, output_root=str(tmp_path / "results")
    ).run([case])

    assert run.passed is True
    assert run.cases[0].passed is True
    assert run.cases[0].workspace_id
    summary = json.loads(
        (tmp_path / "results" / run.run_id / "summary.json").read_text(
            encoding="utf-8"
        )
    )
    assert summary["summary"]["passed"] == 1
    assert summary_path.endswith("summary.json")
    assert (tmp_path / "results" / run.run_id / "runner-smoke" / "trace.jsonl").exists()


def test_real_case_is_skipped_with_mock_providers(tmp_path):
    case = EvalCase(
        case_id="real-only",
        description="",
        input={"topic_background": "test"},
        requires_real_providers=True,
    )
    run, _ = EvalRunner(
        Config(llm_provider="mock", retrieval_provider="mock"),
        output_root=str(tmp_path),
    ).run([case])
    assert run.cases[0].skipped is True
    assert "真实" in run.cases[0].skip_reason


def test_eval_rejects_confirmation_by_default_and_case_can_allow(tmp_path):
    draft = tmp_path / "unstructured.md"
    draft.write_text("Readable academic prose. " * 220, encoding="utf-8")
    runner = EvalRunner(Config(), output_root=str(tmp_path / "results"))

    refused = EvalCase(
        case_id="refused",
        description="",
        input={"draft_path": draft.name},
        source_path=str(tmp_path / "refused.json"),
    )
    with pytest.raises(IngestionConfirmationRequired):
        runner._build_request(refused, tmp_path / "refused")

    allowed = EvalCase(
        case_id="allowed",
        description="",
        input={"draft_path": draft.name, "confirm_ingestion": True},
        source_path=str(tmp_path / "allowed.json"),
    )
    request = runner._build_request(allowed, tmp_path / "allowed")
    assert request.profile["ingestion_quality"]["status"] == (
        "confirmation_required"
    )
