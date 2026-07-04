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
        """收集所有实验 CSV 中的数值——供 QualityGate 做 grounding 检查。

        从每条 ``Experiment.results_data.rows`` 抽出所有数值（可解析为 float 的），
        去重后返回。这是「正文中允许出现的数字」的白名单基础。
        """
        values: set[float] = set()
        for exp in self.experiments:
            rows = exp.results_data.get("rows") or []
            for row in rows:
                if not isinstance(row, dict):
                    continue
                for v in row.values():
                    try:
                        values.add(float(v))
                    except (TypeError, ValueError):
                        continue
        return sorted(values)

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


__all__ = [
    "Contribution",
    "Experiment",
    "MethodSpec",
    "ResearchArtifact",
]
