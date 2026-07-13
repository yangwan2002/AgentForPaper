"""论文评测的确定性指标。

本模块只消费运行结果与最终工作区，不调用 LLM。LLM Judge 可后续作为独立指标
接入，但不能替代引用、数字、格式等可机械验证的硬约束。
"""

from __future__ import annotations

from typing import Any

from paper_agent.tools.faithfulness_extract import cited_reference_ids
from paper_agent.evaluation.models import EvalCase, MetricResult
from paper_agent.orchestrator import PaperResult
from paper_agent.workspace.models import PaperWorkspace


_CITATION_ISSUES = {"invalid_citation", "text_citation_invalid"}


def _faithfulness_audited_count(ws: PaperWorkspace) -> int:
    return sum(
        str(item.get("parse_status", "")).lower() in {"parsed", "repaired"}
        for item in ws.citation_faithfulness
    )


def _grounding_fulltext_hit_rate(ws: PaperWorkspace) -> float:
    cited: set[str] = set()
    for draft in ws.section_drafts.values():
        cited.update(cited_reference_ids(getattr(draft, "content", "") or ""))
    if not cited:
        return 1.0
    lookup: dict[str, object] = {}
    for reference in ws.verified_references:
        lookup[reference.id] = reference
        if reference.source_id:
            lookup[reference.source_id] = reference
        for alias in reference.citation_aliases:
            lookup[alias] = reference
    matched = {
        lookup[ref_id]
        for ref_id in cited
        if ref_id in lookup
    }
    if not matched:
        return 1.0
    with_fulltext = sum(
        1
        for reference in matched
        if (getattr(reference, "full_text", "") or "").strip()
    )
    return round(with_fulltext / len(matched), 6)


def _faithfulness_count(ws: PaperWorkspace, verdict: str) -> int:
    return sum(
        1
        for finding in ws.citation_faithfulness
        if str(finding.get("verdict", "")).lower() == verdict
    )


def diagnostics(
    ws: PaperWorkspace,
    result: PaperResult,
    *,
    duration_s: float,
) -> dict[str, Any]:
    issues = list(ws.quality_report)
    required_evidence = [
        evidence_id
        for node in ws.ordered_sections()
        for evidence_id in node.required_evidence_ids
    ]
    covered_evidence = [
        evidence_id
        for node in ws.ordered_sections()
        for evidence_id in (
            ws.section_drafts.get(node.section_id).evidence_ids
            if ws.section_drafts.get(node.section_id)
            else []
        )
        if evidence_id in node.required_evidence_ids
    ]
    evidence_coverage = (
        len(covered_evidence) / len(required_evidence)
        if required_evidence
        else 1.0
    )
    manifests = [
        manifest
        for draft in ws.section_drafts.values()
        for manifest in draft.claim_manifest
    ]
    final_artifact_violations = 0
    if ws.artifact is not None and not ws.artifact.is_empty():
        from paper_agent.tools.artifact_commit_gate import ArtifactCommitGate

        commit_gate = ArtifactCommitGate()
        for node in ws.ordered_sections():
            draft = ws.section_drafts.get(node.section_id)
            if draft is None and (node.required_evidence_ids or node.allowed_evidence_ids):
                final_artifact_violations += 1
            elif draft is not None and (
                node.required_evidence_ids
                or node.allowed_evidence_ids
                or draft.artifact_hash
            ):
                final_artifact_violations += len(
                    commit_gate.check(ws, node, draft).high_violations
                )
    faithfulness_total = len(ws.citation_faithfulness)
    faithfulness_audited = _faithfulness_audited_count(ws)
    return {
        "duration_s": round(duration_s, 3),
        "section_count": len(ws.section_drafts),
        "empty_sections": sum(1 for issue in issues if issue.get("type") == "empty_section"),
        "high_quality_issues": sum(
            1 for issue in issues if issue.get("severity") == "high"
        ),
        "fabricated_citations": sum(
            1 for issue in issues if issue.get("type") in _CITATION_ISSUES
        ),
        "unverified_source_citations": sum(
            1
            for issue in issues
            if issue.get("type") == "source_citation_unverified"
        ),
        "fabricated_metrics": sum(
            1 for issue in issues if issue.get("type") == "fabricated_metric"
        ),
        "unsupported_citations": _faithfulness_count(ws, "unsupported"),
        "cannot_verify_citations": _faithfulness_count(ws, "cannot_verify"),
        "faithfulness_audited": faithfulness_audited,
        "faithfulness_total_claims": faithfulness_total,
        "faithfulness_audited_ratio": round(
            faithfulness_audited / max(faithfulness_total, 1),
            6,
        ),
        "grounding_fulltext_hit_rate": _grounding_fulltext_hit_rate(ws),
        "verified_references": len(ws.verified_references),
        "iteration": ws.iteration,
        "export_file_count": len(result.export.files) if result.export else 0,
        # 由 EvalRunner 在有 UsageTracker/配置上下文时覆盖。
        "total_tokens": 0,
        "llm_calls": 0,
        "independent_reviewer": False,
        "ingest_rejected": False,
        "artifact_violations": final_artifact_violations,
        "artifact_rejections": len(ws.artifact_violations),
        "evidence_coverage": round(evidence_coverage, 6),
        "claim_evidence_coverage": (
            round(
                sum(bool(item.get("evidence_ids")) for item in manifests)
                / len(manifests),
                6,
            )
            if manifests
            else (1.0 if not required_evidence else 0.0)
        ),
        "ingest_quality_score": (
            (ws.profile.get("ingestion_quality") or {}).get("score", 100)
        ),
    }


def evaluate_assertions(
    case: EvalCase,
    ws: PaperWorkspace,
    result: PaperResult,
    observed: dict[str, Any],
) -> list[MetricResult]:
    """执行案例声明的硬断言；未知断言直接报失败，避免拼写错误被静默忽略。"""

    metrics: list[MetricResult] = []
    assertions = case.assertions

    for name, expected in assertions.items():
        if name == "run_completed":
            actual = bool(result.workspace_id)
        elif name == "export_created":
            actual = bool(result.export and result.export.files)
        elif name == "expected_format":
            actual = result.export.output_format.value if result.export else None
        elif name == "submittable":
            actual = result.submittable
        elif name == "requires_independent_reviewer":
            actual = bool(observed.get("independent_reviewer", False))
        elif name == "ingest_rejected":
            actual = bool(observed.get("ingest_rejected", False))
        elif name == "terminated_reason_in":
            actual = result.terminated_reason
            allowed = [str(item) for item in expected]
            metrics.append(
                MetricResult(
                    name=name,
                    passed=actual in allowed,
                    actual=actual,
                    expected=allowed,
                )
            )
            continue
        elif name.startswith("max_"):
            diagnostic_name = name[4:]
            if diagnostic_name not in observed:
                metrics.append(_unknown_assertion(name, expected))
                continue
            actual = observed[diagnostic_name]
            metrics.append(
                MetricResult(
                    name=name,
                    passed=actual <= expected,
                    actual=actual,
                    expected=expected,
                )
            )
            continue
        elif name.startswith("min_"):
            diagnostic_name = name[4:]
            if diagnostic_name not in observed:
                metrics.append(_unknown_assertion(name, expected))
                continue
            actual = observed[diagnostic_name]
            metrics.append(
                MetricResult(
                    name=name,
                    passed=actual >= expected,
                    actual=actual,
                    expected=expected,
                )
            )
            continue
        elif name == "required_sections":
            available = [
                f"{node.section_id} {node.title}".lower() for node in ws.ordered_sections()
            ]
            missing = [
                str(item)
                for item in expected
                if not any(str(item).lower() in section for section in available)
            ]
            metrics.append(
                MetricResult(
                    name=name,
                    passed=not missing,
                    actual={"available": available, "missing": missing},
                    expected=list(expected),
                )
            )
            continue
        elif name == "forbid_terms":
            paper = "\n".join(
                draft.content for draft in ws.section_drafts.values()
            ).lower()
            found = [str(term) for term in expected if str(term).lower() in paper]
            metrics.append(
                MetricResult(
                    name=name,
                    passed=not found,
                    actual=found,
                    expected=[],
                )
            )
            continue
        elif name == "required_terms":
            paper = "\n".join(
                draft.content for draft in ws.section_drafts.values()
            ).lower()
            missing = [
                str(term) for term in expected if str(term).lower() not in paper
            ]
            metrics.append(
                MetricResult(
                    name=name,
                    passed=not missing,
                    actual={"missing": missing},
                    expected=list(expected),
                )
            )
            continue
        else:
            metrics.append(_unknown_assertion(name, expected))
            continue

        metrics.append(
            MetricResult(
                name=name,
                passed=actual == expected,
                actual=actual,
                expected=expected,
            )
        )

    return metrics


def _unknown_assertion(name: str, expected: Any) -> MetricResult:
    return MetricResult(
        name=name,
        passed=False,
        expected=expected,
        details=f"未知评测断言：{name}",
    )


__all__ = ["diagnostics", "evaluate_assertions"]
