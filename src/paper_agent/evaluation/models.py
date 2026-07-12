"""评测案例与结果的数据模型。

评测文本不是固定答案：开放式论文生成应通过事实、引用、结构和交付约束来判定，
而不是与某一份参考文章做逐字匹配。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class EvalCase:
    case_id: str
    description: str
    input: dict[str, Any]
    assertions: dict[str, Any] = field(default_factory=dict)
    tags: list[str] = field(default_factory=list)
    requires_real_providers: bool = False
    source_path: str = ""

    @classmethod
    def from_dict(cls, data: dict[str, Any], *, source_path: str = "") -> "EvalCase":
        case_id = str(data.get("id") or "").strip()
        if not case_id:
            raise ValueError("评测案例缺少非空 id")
        input_data = data.get("input")
        if not isinstance(input_data, dict):
            raise ValueError(f"评测案例 {case_id} 的 input 必须是对象")
        assertions = data.get("assertions") or {}
        if not isinstance(assertions, dict):
            raise ValueError(f"评测案例 {case_id} 的 assertions 必须是对象")
        return cls(
            case_id=case_id,
            description=str(data.get("description") or ""),
            input=dict(input_data),
            assertions=dict(assertions),
            tags=[str(tag) for tag in (data.get("tags") or [])],
            requires_real_providers=bool(data.get("requires_real_providers", False)),
            source_path=source_path,
        )


@dataclass
class MetricResult:
    name: str
    passed: bool
    actual: Any = None
    expected: Any = None
    details: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "passed": self.passed,
            "actual": self.actual,
            "expected": self.expected,
            "details": self.details,
        }


@dataclass
class EvalCaseResult:
    case_id: str
    passed: bool
    skipped: bool = False
    skip_reason: str = ""
    error: str = ""
    duration_s: float = 0.0
    workspace_id: str = ""
    terminated_reason: str = ""
    submittable: bool | None = None
    export_files: list[str] = field(default_factory=list)
    metrics: list[MetricResult] = field(default_factory=list)
    diagnostics: dict[str, Any] = field(default_factory=dict)
    usage: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "case_id": self.case_id,
            "passed": self.passed,
            "skipped": self.skipped,
            "skip_reason": self.skip_reason,
            "error": self.error,
            "duration_s": round(self.duration_s, 3),
            "workspace_id": self.workspace_id,
            "terminated_reason": self.terminated_reason,
            "submittable": self.submittable,
            "export_files": list(self.export_files),
            "metrics": [metric.to_dict() for metric in self.metrics],
            "diagnostics": dict(self.diagnostics),
            "usage": dict(self.usage),
        }


@dataclass
class EvalRunResult:
    run_id: str
    started_at: str
    config: dict[str, Any]
    cases: list[EvalCaseResult] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        executed = [case for case in self.cases if not case.skipped]
        return bool(executed) and all(case.passed for case in executed)

    def to_dict(self) -> dict[str, Any]:
        executed = [case for case in self.cases if not case.skipped]
        return {
            "run_id": self.run_id,
            "started_at": self.started_at,
            "passed": self.passed,
            "summary": {
                "total": len(self.cases),
                "executed": len(executed),
                "passed": sum(1 for case in executed if case.passed),
                "failed": sum(1 for case in executed if not case.passed),
                "skipped": sum(1 for case in self.cases if case.skipped),
            },
            "config": dict(self.config),
            "cases": [case.to_dict() for case in self.cases],
        }


__all__ = [
    "EvalCase",
    "MetricResult",
    "EvalCaseResult",
    "EvalRunResult",
]
