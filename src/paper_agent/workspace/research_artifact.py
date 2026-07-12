"""用户研究内容的结构化输入（Round 7：反 hallucination 的源头修复）。

设计动机：GENERATION 模式此前只接受 ``topic_background`` 字符串——LLM 只能凭空
编造方法、数据集、超参、实验结果。所有后续修复（Round 4 对抗审 / Round 5
section-typed / Round 6 fetch_paper_section）都只让 LLM 「编得更专业」，没解决
「编的内容不是用户的研究」这个根因。

本模块定义用户真实研究的结构化输入，由 `ingestion/artifact_loader.py` 从
YAML + CSV 加载。写作智能体把 artifact 注入 prompt，质量闸据 artifact 检查
正文数字是否 grounded 在真实实验数据，对抗审把"正文写到但 artifact 没有的方法/
数据集/结果"标为 ``fabricated_content``。

所有 dataclass 均纯数据、可 to_dict / from_dict 序列化，与现有 ``PaperWorkspace``
序列化口径一致。
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field


@dataclass
class Contribution:
    """一条 contribution 声明（最终在 Introduction / Conclusion 被复述）。

    ``summary`` 是面向论文的一句话；``evidence_refs`` 指向支撑它的 experiment id
    或 section_id（如 ``["main", "ablation_loss"]``），供质量闸客观检查"contribution
    必须有兑现"。
    """

    summary: str
    evidence_refs: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "summary": self.summary,
            "evidence_refs": list(self.evidence_refs),
        }

    @classmethod
    def from_dict(cls, data: dict) -> "Contribution":
        return cls(
            summary=str(data.get("summary", "")).strip(),
            evidence_refs=[str(x) for x in (data.get("evidence_refs") or [])],
        )


@dataclass
class Experiment:
    """一次实验的完整描述——是 ground truth 数据。

    ``results_csv`` 指向 CSV 文件路径（相对 artifact 目录），系统读它算
    mean / std；正文里所有数字必须能在某个 ``Experiment.results_data`` 中找到，
    否则 ``QualityGate.check_grounding`` 判 fabricated_metric。

    ``results_data`` 在 loader 中由 CSV 解析填入（不直接由用户写），结构：
    ``{"columns": [...], "rows": [{col: val, ...}, ...], "stats": {col: {"mean": x, "std": y, "min": ..., "max": ...}}}``。
    """

    experiment_id: str
    dataset: str = ""
    baselines: list[str] = field(default_factory=list)
    metrics: list[str] = field(default_factory=list)
    hyperparameters: dict = field(default_factory=dict)
    seed: int | None = None
    hardware: str = ""
    results_csv: str = ""
    results_data: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "experiment_id": self.experiment_id,
            "dataset": self.dataset,
            "baselines": list(self.baselines),
            "metrics": list(self.metrics),
            "hyperparameters": dict(self.hyperparameters),
            "seed": self.seed,
            "hardware": self.hardware,
            "results_csv": self.results_csv,
            "results_data": dict(self.results_data),
        }

    @classmethod
    def from_dict(cls, data: dict) -> "Experiment":
        return cls(
            experiment_id=str(data.get("experiment_id", "")).strip(),
            dataset=str(data.get("dataset", "")),
            baselines=[str(b) for b in (data.get("baselines") or [])],
            metrics=[str(m) for m in (data.get("metrics") or [])],
            hyperparameters=dict(data.get("hyperparameters") or {}),
            seed=data.get("seed"),
            hardware=str(data.get("hardware", "")),
            results_csv=str(data.get("results_csv", "")),
            results_data=dict(data.get("results_data") or {}),
        )


@dataclass
class MethodSpec:
    """方法的结构化描述。

    用户最低限度需提供 ``overview``（一两段自然语言）；``key_components`` 与
    ``formal_definition`` 让 Method 章节更具体可复现。``pseudocode`` 可空——
    存在时直接注入到 Method 章节正文。
    """

    overview: str
    key_components: list[str] = field(default_factory=list)
    formal_definition: str = ""
    pseudocode: str = ""

    def to_dict(self) -> dict:
        return {
            "overview": self.overview,
            "key_components": list(self.key_components),
            "formal_definition": self.formal_definition,
            "pseudocode": self.pseudocode,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "MethodSpec":
        return cls(
            overview=str(data.get("overview", "")).strip(),
            key_components=[str(k) for k in (data.get("key_components") or [])],
            formal_definition=str(data.get("formal_definition", "")),
            pseudocode=str(data.get("pseudocode", "")),
        )


@dataclass
class ResearchArtifact:
    """用户提供的真实研究内容（GENERATION 模式建议必备）。

    无 artifact → 编排器会显式降级，标注「以下产出由 LLM 推断，可能与作者实际
    研究不符」，并跳过 grounding 检查（因为没有真实数据可校验）。

    必填校验由 loader 完成；本数据类只描述结构，不做校验（dataclass 应是纯
    数据，运行期校验在 loader 层做更易测试）。

    Attributes:
        research_question: 一句话研究问题（必填，loader 校验）。
        method: 方法结构化描述（必填，loader 校验 ``method.overview`` 非空）。
        contributions: 3-5 条 contribution（必填，loader 校验非空）。
        experiments: 至少一条实验（必填，loader 校验非空）。
        code_repository: 代码仓库 URL（可选，写 reproducibility 段用）。
        novelty_claims: 关键新颖性声明（可选，对抗审会针对这些做 contrast 验证）。
        must_cite_refs: 用户指定必须引用的 reference id 列表（可选）。
        notes: 自由格式补充说明，loader 从 notes.md 加载（可选）。
        artifact_dir: 加载时的根目录绝对路径（loader 填，便于解析 CSV 相对路径）。
    """

    research_question: str
    method: MethodSpec
    contributions: list[Contribution] = field(default_factory=list)
    experiments: list[Experiment] = field(default_factory=list)
    code_repository: str = ""
    novelty_claims: list[str] = field(default_factory=list)
    must_cite_refs: list[str] = field(default_factory=list)
    notes: str = ""
    artifact_dir: str = ""

    # --- 便捷查询 ---

    def is_empty(self) -> bool:
        """是否「空 artifact」——用于编排器决定是否走降级标注。"""
        return (
            not self.research_question.strip()
            and not self.method.overview.strip()
            and not self.contributions
            and not self.experiments
        )

    def all_numeric_values(self) -> list[float]:
        """收集所有实验数据和超参数中的数值——供 QualityGate 做 grounding 检查。

        从每条 ``Experiment.results_data.rows`` 与 ``hyperparameters`` 递归抽出
        所有数值（可解析为 float 的），去重后返回。``stats`` 的派生值由
        ``build_allowed_values`` 单独处理，避免重复引入经格式化后不稳定的中间值。
        """
        values: set[float] = set()

        def collect(value: object) -> None:
            if isinstance(value, dict):
                for nested in value.values():
                    collect(nested)
                return
            if isinstance(value, (list, tuple)):
                for nested in value:
                    collect(nested)
                return
            try:
                values.add(float(value))
            except (TypeError, ValueError):
                return

        for exp in self.experiments:
            collect(exp.hyperparameters)
            collect((exp.results_data or {}).get("rows") or [])
        return sorted(values)

    def contract(self) -> "ArtifactContract":
        """Build the immutable, verifiable contract consumed by downstream agents."""
        return ArtifactContract.from_artifact(self)

    # --- 序列化 ---

    def to_dict(self) -> dict:
        return {
            "research_question": self.research_question,
            "method": self.method.to_dict(),
            "contributions": [c.to_dict() for c in self.contributions],
            "experiments": [e.to_dict() for e in self.experiments],
            "code_repository": self.code_repository,
            "novelty_claims": list(self.novelty_claims),
            "must_cite_refs": list(self.must_cite_refs),
            "notes": self.notes,
            "artifact_dir": self.artifact_dir,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "ResearchArtifact":
        return cls(
            research_question=str(data.get("research_question", "")).strip(),
            method=MethodSpec.from_dict(data.get("method") or {}),
            contributions=[
                Contribution.from_dict(c) for c in (data.get("contributions") or [])
            ],
            experiments=[
                Experiment.from_dict(e) for e in (data.get("experiments") or [])
            ],
            code_repository=str(data.get("code_repository", "")),
            novelty_claims=[str(n) for n in (data.get("novelty_claims") or [])],
            must_cite_refs=[str(r) for r in (data.get("must_cite_refs") or [])],
            notes=str(data.get("notes", "")),
            artifact_dir=str(data.get("artifact_dir", "")),
        )


def _slug(value: str) -> str:
    normalized = re.sub(r"[^a-zA-Z0-9\u4e00-\u9fff]+", "-", value.strip().lower())
    return normalized.strip("-") or "unnamed"


@dataclass(frozen=True)
class ArtifactContract:
    """Immutable projection of user facts used for planning and commit checks."""

    artifact_hash: str
    evidence: dict[str, str]
    allowed_entities: frozenset[str]
    allowed_numeric_values: tuple[float, ...]
    # entity -> evidence ids that define/measure that entity.
    entity_evidence: dict[str, tuple[str, ...]]
    # contribution/novelty evidence may link to concrete experiments.
    evidence_links: dict[str, tuple[str, ...]]
    experiment_ids: tuple[str, ...]
    required_citations: frozenset[str]
    complete: bool

    @classmethod
    def from_artifact(cls, artifact: ResearchArtifact) -> "ArtifactContract":
        raw = artifact.to_dict()
        raw.pop("artifact_dir", None)
        digest = hashlib.sha256(
            json.dumps(raw, ensure_ascii=False, sort_keys=True).encode("utf-8")
        ).hexdigest()

        evidence: dict[str, str] = {
            "research:question": artifact.research_question,
            "method:overview": artifact.method.overview,
        }
        entities: set[str] = set()
        entity_evidence: dict[str, set[str]] = {}
        evidence_links: dict[str, tuple[str, ...]] = {}

        def bind_entity(entity: str, evidence_id: str) -> None:
            normalized = entity.strip()
            if len(normalized) < 2:
                return
            entities.add(normalized)
            entity_evidence.setdefault(normalized.lower(), set()).add(evidence_id)
            for token in re.findall(r"[A-Za-z][A-Za-z0-9+_.-]{1,39}", normalized):
                if token.lower() in {
                    "and", "with", "from", "using", "method", "model",
                    "network", "module", "dataset", "metric",
                }:
                    continue
                entities.add(token)
                entity_evidence.setdefault(token.lower(), set()).add(evidence_id)

        def bind_named_tokens(text: str, evidence_id: str) -> None:
            for token in re.findall(r"[A-Za-z][A-Za-z0-9+_.-]{1,39}", text):
                if token.lower() in {
                    "and", "with", "from", "using", "method", "model",
                    "network", "module", "dataset", "metric", "the",
                }:
                    continue
                bind_entity(token, evidence_id)

        for index, component in enumerate(artifact.method.key_components):
            evidence_id = f"method:component:{_slug(component)}"
            evidence[evidence_id] = component
            bind_entity(component, evidence_id)
        bind_named_tokens(artifact.method.overview, "method:overview")
        for index, contribution in enumerate(artifact.contributions):
            evidence_id = f"contribution:{index}"
            evidence[evidence_id] = contribution.summary
            bind_named_tokens(contribution.summary, evidence_id)
            evidence_links[evidence_id] = tuple(
                ref if ref.startswith("experiment:") else f"experiment:{ref}"
                for ref in contribution.evidence_refs
            )
        experiment_ids: list[str] = []
        experiments_complete = bool(artifact.experiments)
        for experiment in artifact.experiments:
            exp_id = experiment.experiment_id or f"experiment-{len(experiment_ids)}"
            experiment_ids.append(exp_id)
            experiment_evidence_id = f"experiment:{exp_id}"
            evidence[experiment_evidence_id] = (
                "实验事实："
                + json.dumps(
                    {
                        "dataset": experiment.dataset,
                        "baselines": experiment.baselines,
                        "metrics": experiment.metrics,
                        "hyperparameters": experiment.hyperparameters,
                        "seed": experiment.seed,
                        "hardware": experiment.hardware,
                        "rows": (experiment.results_data or {}).get("rows") or [],
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                )
            )
            if experiment.dataset.strip():
                evidence_id = f"dataset:{exp_id}"
                evidence[evidence_id] = experiment.dataset.strip()
                bind_entity(experiment.dataset, evidence_id)
                entity_evidence.setdefault(
                    experiment.dataset.strip().lower(), set()
                ).add(experiment_evidence_id)
            for baseline in experiment.baselines:
                evidence_id = f"baseline:{exp_id}:{_slug(baseline)}"
                evidence[evidence_id] = baseline
                bind_entity(baseline, evidence_id)
                entity_evidence.setdefault(baseline.strip().lower(), set()).add(
                    experiment_evidence_id
                )
            for metric in experiment.metrics:
                evidence_id = f"metric:{exp_id}:{_slug(metric)}"
                evidence[evidence_id] = metric
                bind_entity(metric, evidence_id)
                entity_evidence.setdefault(metric.strip().lower(), set()).add(
                    experiment_evidence_id
                )
            rows = (experiment.results_data or {}).get("rows") or []
            experiments_complete = experiments_complete and bool(rows)
        for index, claim in enumerate(artifact.novelty_claims):
            evidence_id = f"novelty:{index}"
            evidence[evidence_id] = claim
            bind_named_tokens(claim, evidence_id)
        for reference_id in artifact.must_cite_refs:
            evidence[f"citation:{reference_id}"] = reference_id

        complete = bool(
            artifact.research_question.strip()
            and artifact.method.overview.strip()
            and artifact.contributions
            and experiments_complete
        )
        return cls(
            artifact_hash=digest,
            evidence=evidence,
            allowed_entities=frozenset(entities),
            allowed_numeric_values=tuple(_build_allowed_numeric_values(artifact)),
            entity_evidence={
                key: tuple(sorted(value))
                for key, value in sorted(entity_evidence.items())
            },
            evidence_links=evidence_links,
            experiment_ids=tuple(experiment_ids),
            required_citations=frozenset(artifact.must_cite_refs),
            complete=complete,
        )

    def evidence_ids_for(self, section_id: str, title: str) -> list[str]:
        """Return deterministic evidence scope for a manuscript section."""
        key = f"{section_id} {title}".lower()
        if any(token in key for token in ("intro", "引言", "绪论")):
            prefixes = ("research:", "contribution:", "citation:")
        elif any(token in key for token in ("experiment", "result", "实验", "结果", "消融")):
            prefixes = ("experiment:", "dataset:", "baseline:", "metric:")
        elif any(token in key for token in ("conclusion", "结论", "总结", "讨论")):
            prefixes = ("contribution:", "experiment:", "novelty:")
        elif any(
            token in key
            for token in (
                "related",
                "background",
                "references",
                "相关",
                "理论",
                "背景",
                "参考文献",
            )
        ):
            prefixes = ("research:", "citation:")
        else:
            prefixes = ("method:", "novelty:", "dataset:", "experiment:")
        return sorted(key for key in self.evidence if key.startswith(prefixes))

    def required_evidence_ids_for(self, section_id: str, title: str) -> list[str]:
        """Minimal evidence that must be visibly realised in a section."""
        key = f"{section_id} {title}".lower()
        if any(token in key for token in ("intro", "引言", "绪论")):
            prefixes = ("research:", "contribution:")
        elif any(token in key for token in ("experiment", "result", "实验", "结果", "消融")):
            prefixes = ("experiment:",)
        elif any(token in key for token in ("conclusion", "结论", "总结", "讨论")):
            prefixes = ("contribution:", "novelty:")
        elif any(
            token in key for token in ("related", "references", "相关", "参考文献")
        ):
            prefixes = ("citation:",)
        else:
            prefixes = ("method:",)
        return sorted(key for key in self.evidence if key.startswith(prefixes))

    def compact_context(self) -> str:
        lines = [
            f"artifact_hash={self.artifact_hash}",
            f"complete={str(self.complete).lower()}",
            "allowed_numeric_values="
            + json.dumps(self.allowed_numeric_values[:256], ensure_ascii=False),
            "allowed_entities="
            + json.dumps(sorted(self.allowed_entities), ensure_ascii=False),
            "evidence:",
        ]
        lines.extend(
            f"- {key}: {value[:2000]}" for key, value in self.evidence.items()
        )
        return "\n".join(lines)


def _build_allowed_numeric_values(artifact: ResearchArtifact) -> list[float]:
    """Build the canonical numeric whitelist stored in ArtifactContract."""
    allowed: set[float] = set(artifact.all_numeric_values())
    for experiment in artifact.experiments:
        rows = (experiment.results_data or {}).get("rows") or []
        numeric_columns: dict[str, list[float]] = {}
        for row in rows:
            if not isinstance(row, dict):
                continue
            for key, raw in row.items():
                try:
                    numeric_columns.setdefault(str(key), []).append(float(raw))
                except (TypeError, ValueError):
                    continue
        for values in numeric_columns.values():
            for index, left in enumerate(values):
                for right in values[index + 1 :]:
                    allowed.add(abs(left - right))
        stats = (experiment.results_data or {}).get("stats") or {}
        for value in stats.values():
            if not isinstance(value, dict):
                continue
            mean = value.get("mean")
            std = value.get("std")
            if mean is not None:
                allowed.add(float(mean))
            if mean is not None and std is not None:
                allowed.add(float(mean) - float(std))
                allowed.add(float(mean) + float(std))
            for key in ("min", "max"):
                if value.get(key) is not None:
                    allowed.add(float(value[key]))
    for value in list(allowed):
        if 0 <= abs(value) <= 1:
            allowed.add(value * 100)
    return sorted(allowed)


__all__ = [
    "Contribution",
    "Experiment",
    "MethodSpec",
    "ResearchArtifact",
    "ArtifactContract",
]
