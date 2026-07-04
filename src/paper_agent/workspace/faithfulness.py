"""引用忠实性审计数据模型（citation-faithfulness-audit）。

本模块只包含纯数据定义，零 I/O、零外部副作用：

- ``FaithfulnessVerdict``：声明级引用忠实性裁决枚举。
- ``ClaimCitationPair``：抽取阶段的瞬态数据（声明句-引用对），不参与序列化。
- ``CitationFaithfulnessFinding``：可序列化的审计发现，经 ``to_dict`` / ``from_dict``
  以 ``list[dict]`` 形态持久化到 ``PaperWorkspace.citation_faithfulness``。

设计要点：
- ``from_dict`` 容错：未知/缺失的 verdict 回落 ``cannot_verify``，忽略未知键，
  保证旧版本 JSON 反序列化不失败（向后兼容，Req 5.3/5.4）。
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class FaithfulnessVerdict(str, Enum):
    """声明级引用忠实性裁决（Req 4.1）。"""

    SUPPORTED = "supported"          # 被引文献支撑该声明句
    WEAK_SUPPORT = "weak_support"    # 仅弱支撑
    UNSUPPORTED = "unsupported"      # 不支撑
    CANNOT_VERIFY = "cannot_verify"  # 无法判定（grounding 不足 / 降级路径）

    @classmethod
    def _missing_(cls, value: object) -> "FaithfulnessVerdict":
        """未知/非法取值一律回落 cannot_verify（容错，绝不臆测 supported）。"""
        return cls.CANNOT_VERIFY


@dataclass
class ClaimCitationPair:
    """声明句-引用对（瞬态，不序列化）。

    抽取阶段用于驱动 grounding 组装与判定流程，不写入工作区。
    """

    section_id: str
    claim_sentence: str
    cited_reference_id: str


@dataclass
class CitationFaithfulnessFinding:
    """可序列化的引用忠实性发现（Req 5.1）。"""

    section_id: str
    cited_reference_id: str
    claim_excerpt: str                       # Claim_Sentence 的截断摘要
    verdict: FaithfulnessVerdict
    severity: str                            # high | medium | low | none
    rationale: str = ""
    supporting_snippet: str = ""
    parse_status: str = ""                   # ParseStatus.value；未调判定器时为 "" / "n/a"
    unverified_reference: bool = False

    def to_dict(self) -> dict:
        """序列化为纯 dict（verdict 用枚举 .value）。"""
        return {
            "section_id": self.section_id,
            "cited_reference_id": self.cited_reference_id,
            "claim_excerpt": self.claim_excerpt,
            "verdict": self.verdict.value,
            "severity": self.severity,
            "rationale": self.rationale,
            "supporting_snippet": self.supporting_snippet,
            "parse_status": self.parse_status,
            "unverified_reference": self.unverified_reference,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "CitationFaithfulnessFinding":
        """容错反序列化。

        - 未知/缺失的 verdict 回落 ``cannot_verify``。
        - 忽略未知键（只取本 dataclass 已声明的字段）。
        - 缺失的可选字段使用默认值。
        """
        data = data or {}
        return cls(
            section_id=data.get("section_id", ""),
            cited_reference_id=data.get("cited_reference_id", ""),
            claim_excerpt=data.get("claim_excerpt", ""),
            verdict=FaithfulnessVerdict(data.get("verdict")),
            severity=data.get("severity", ""),
            rationale=data.get("rationale", ""),
            supporting_snippet=data.get("supporting_snippet", ""),
            parse_status=data.get("parse_status", ""),
            unverified_reference=bool(data.get("unverified_reference", False)),
        )
