"""可重复、可批量执行的论文 Agent 评测运行器。"""

from __future__ import annotations

import json
import os
import subprocess
import time
import uuid
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

from paper_agent.app import build_from_config
from paper_agent.config import Config
from paper_agent.evaluation.metrics import diagnostics, evaluate_assertions
from paper_agent.evaluation.models import (
    EvalCase,
    EvalCaseResult,
    EvalRunResult,
    MetricResult,
)
from paper_agent.ingestion import load_artifact, load_document_with_quality
from paper_agent.providers.factory import reviewer_model_is_diverse
from paper_agent.observability.sinks import JsonLinesSink
from paper_agent.observability.usage import UsageTracker
from paper_agent.orchestrator import PaperRequest
from paper_agent.workspace.models import OutputFormat
from paper_agent.workspace.research_artifact import ResearchArtifact
from paper_agent.workspace.store import JsonFileStore


def discover_cases(path: str) -> list[EvalCase]:
    """加载单个 JSON 案例或目录下全部 ``*.json`` 案例。"""

    root = Path(path)
    files = [root] if root.is_file() else sorted(root.rglob("*.json"))
    cases: list[EvalCase] = []
    seen: set[str] = set()
    for file_path in files:
        with file_path.open("r", encoding="utf-8") as fh:
            raw = json.load(fh)
        case = EvalCase.from_dict(raw, source_path=str(file_path.resolve()))
        if case.case_id in seen:
            raise ValueError(f"重复评测案例 id：{case.case_id}")
        seen.add(case.case_id)
        cases.append(case)
    if not cases:
        raise ValueError(f"未找到评测案例：{path}")
    return cases


class EvalRunner:
    def __init__(
        self,
        config: Config,
        *,
        output_root: str = "eval/results",
    ) -> None:
        self._config = config
        self._output_root = Path(output_root)

    def run(self, cases: list[EvalCase]) -> tuple[EvalRunResult, str]:
        started = datetime.now(timezone.utc)
        run_id = started.strftime("%Y%m%dT%H%M%SZ") + "-" + uuid.uuid4().hex[:6]
        run_dir = self._output_root / run_id
        run_dir.mkdir(parents=True, exist_ok=True)

        run_result = EvalRunResult(
            run_id=run_id,
            started_at=started.isoformat(),
            config=self._public_config(),
        )
        for case in cases:
            case_result = self._run_case(case, run_dir)
            run_result.cases.append(case_result)
            _write_json(
                run_dir / f"{_safe_name(case.case_id)}.json",
                case_result.to_dict(),
            )

        summary_path = run_dir / "summary.json"
        _write_json(summary_path, run_result.to_dict())
        return run_result, str(summary_path)

    def _run_case(self, case: EvalCase, run_dir: Path) -> EvalCaseResult:
        if case.requires_real_providers and (
            self._config.llm_provider == "mock"
            or self._config.retrieval_provider == "mock"
        ):
            return EvalCaseResult(
                case_id=case.case_id,
                passed=False,
                skipped=True,
                skip_reason="案例要求真实 LLM 与真实检索 provider",
            )

        started = time.monotonic()
        case_dir = run_dir / _safe_name(case.case_id)
        workspace_dir = case_dir / "workspace"
        case_dir.mkdir(parents=True, exist_ok=True)
        tracker = UsageTracker()

        try:
            try:
                request = self._build_request(case, case_dir)
            except Exception as exc:
                if case.assertions.get("ingest_rejected") is True:
                    metric = MetricResult(
                        name="ingest_rejected",
                        passed=True,
                        actual=True,
                        expected=True,
                        details=f"{type(exc).__name__}: {exc}",
                    )
                    metrics = [metric]
                    for name, expected in case.assertions.items():
                        if name == "ingest_rejected":
                            continue
                        metrics.append(
                            MetricResult(
                                name=name,
                                passed=False,
                                expected=expected,
                                details="摄入已拒绝，无法计算该运行后断言",
                            )
                        )
                    return EvalCaseResult(
                        case_id=case.case_id,
                        passed=all(item.passed for item in metrics),
                        duration_s=time.monotonic() - started,
                        metrics=metrics,
                        diagnostics={"ingest_rejected": True},
                        usage=_usage_dict(tracker),
                    )
                raise
            case_config = replace(
                self._config,
                workspace_dir=str(workspace_dir),
                default_output_format=request.output_format
                or self._config.default_output_format,
            )
            store = JsonFileStore(str(workspace_dir))
            sink = JsonLinesSink(
                path=str(case_dir / "trace.jsonl"),
                content_level=case_config.trace_content_level,
            )
            orchestrator = build_from_config(
                case_config,
                store=store,
                sink=sink,
                tracker=tracker,
            )
            paper_result = orchestrator.run(request)
            ws = store.load(paper_result.workspace_id)
            if ws is None:
                raise RuntimeError("评测运行结束后无法加载工作区")

            duration_s = time.monotonic() - started
            observed = diagnostics(ws, paper_result, duration_s=duration_s)
            observed.update(
                {
                    "total_tokens": tracker.total_tokens,
                    "llm_calls": tracker.calls,
                    "independent_reviewer": self._has_independent_reviewer(),
                    "ingest_rejected": False,
                }
            )
            metrics = evaluate_assertions(case, ws, paper_result, observed)
            return EvalCaseResult(
                case_id=case.case_id,
                passed=all(metric.passed for metric in metrics),
                duration_s=duration_s,
                workspace_id=paper_result.workspace_id,
                terminated_reason=paper_result.terminated_reason,
                submittable=paper_result.submittable,
                export_files=list(paper_result.export.files)
                if paper_result.export
                else [],
                metrics=metrics,
                diagnostics=observed,
                usage=_usage_dict(tracker),
            )
        except Exception as exc:  # noqa: BLE001 - 单案例失败不应中止整个 suite
            return EvalCaseResult(
                case_id=case.case_id,
                passed=False,
                error=f"{type(exc).__name__}: {exc}",
                duration_s=time.monotonic() - started,
                usage=_usage_dict(tracker),
            )

    def _build_request(self, case: EvalCase, case_dir: Path) -> PaperRequest:
        data = case.input
        source_dir = Path(case.source_path).parent if case.source_path else Path.cwd()

        draft = data.get("draft")
        draft_path = data.get("draft_path")
        ingestion_quality = None
        if draft is not None and draft_path:
            raise ValueError(f"案例 {case.case_id} 不可同时提供 draft 与 draft_path")
        if draft_path:
            resolved = _resolve_path(str(draft_path), source_dir)
            draft, ingestion_quality = load_document_with_quality(
                str(resolved),
                asset_dir=str(case_dir / "ingested_assets"),
                confirm=bool(data.get("confirm_ingestion", False)),
            )

        artifact = None
        artifact_data = data.get("artifact")
        artifact_dir = data.get("artifact_dir")
        if artifact_data is not None and artifact_dir:
            raise ValueError(
                f"案例 {case.case_id} 不可同时提供 artifact 与 artifact_dir"
            )
        if artifact_data is not None:
            if not isinstance(artifact_data, dict):
                raise ValueError(f"案例 {case.case_id} 的 artifact 必须是对象")
            artifact = ResearchArtifact.from_dict(artifact_data)
        elif artifact_dir:
            artifact = load_artifact(
                str(_resolve_path(str(artifact_dir), source_dir))
            )

        output_format = OutputFormat(
            str(data.get("output_format") or self._config.default_output_format.value)
        )
        profile = dict(data.get("profile") or {})
        if ingestion_quality is not None:
            profile["ingestion_quality"] = ingestion_quality.to_profile()
        return PaperRequest(
            draft=str(draft) if draft is not None else None,
            topic_background=data.get("topic_background"),
            output_format=output_format,
            figures=list(data.get("figures") or []),
            profile=profile,
            artifact=artifact,
        )

    def _public_config(self) -> dict:
        return {
            "git_commit": _git_commit(),
            "llm_provider": self._config.llm_provider,
            "llm_model": self._config.llm_model,
            "retrieval_provider": self._config.retrieval_provider,
            "reviewer_llm_provider": self._config.reviewer_llm_provider,
            "reviewer_llm_model": self._config.reviewer_llm_model,
            "require_reviewer_model_diversity": (
                self._config.require_reviewer_model_diversity
            ),
            "iteration_limit": self._config.iteration_limit,
            "quality_threshold": self._config.quality_threshold,
            "total_token_budget": self._config.total_token_budget,
            "total_llm_call_budget": self._config.total_llm_call_budget,
            "wall_clock_deadline_s": self._config.wall_clock_deadline_s,
            "review_token_budget": self._config.review_token_budget,
            "citation_faithfulness_enabled": (
                self._config.citation_faithfulness_enabled
            ),
            "grounding_fulltext_enabled": self._config.grounding_fulltext_enabled,
            "faithfulness_max_claims": self._config.faithfulness_max_claims,
            "faithfulness_screen_deadline_s": (
                self._config.faithfulness_screen_deadline_s
            ),
            "faithfulness_token_budget": self._config.faithfulness_token_budget,
            "independent_reviewer": self._has_independent_reviewer(),
            "trace_content_level": self._config.trace_content_level,
        }

    def _has_independent_reviewer(self) -> bool:
        return reviewer_model_is_diverse(self._config)


def _resolve_path(raw: str, source_dir: Path) -> Path:
    path = Path(raw)
    return path if path.is_absolute() else (source_dir / path).resolve()


def _safe_name(value: str) -> str:
    safe = "".join(char for char in value if char.isalnum() or char in "-_")
    return safe[:100] or "case"


def _usage_dict(tracker: UsageTracker) -> dict:
    return {
        "calls": tracker.calls,
        "prompt_tokens": tracker.prompt_tokens,
        "completion_tokens": tracker.completion_tokens,
        "total_tokens": tracker.total_tokens,
        "estimated": tracker.estimated,
        "by_role": {
            role: {
                "calls": usage.calls,
                "prompt_tokens": usage.prompt_tokens,
                "completion_tokens": usage.completion_tokens,
                "total_tokens": usage.total_tokens,
            }
            for role, usage in sorted(tracker.by_role.items())
        },
    }


def _git_commit() -> str:
    try:
        completed = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
            shell=False,
        )
        return completed.stdout.strip() if completed.returncode == 0 else ""
    except (OSError, subprocess.TimeoutExpired):
        return ""


def _write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


__all__ = ["EvalRunner", "discover_cases"]
