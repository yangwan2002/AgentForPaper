"""可投递性判定（submittability verdict）。

「直接输出可投递论文」是本系统的目标，但现实里有若干**硬约束**决定一份产物
是否真的可以投递。本模块把这些约束聚合成一个显式、结构化的判定，避免把
「LLM 推断版 / 未通过格式校验 / 质量未达标」的产物误当成可投递成品。

判定是**保守**的：任一硬约束不满足即标记为不可投递（``submittable=False``），
并给出可读原因；软风险（如文本重合）记为 caution，不单独否决可投递性，但会列入
原因供人工复核。

本模块纯数据、不调用 LLM、不改动工作区——只读 workspace + 终止原因 + 导出说明
+ 查重 findings，产出一个 ``SubmittabilityVerdict``。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from paper_agent.workspace.models import InputMode, PaperWorkspace

# 导出说明中代表「格式未通过/未校验」的判据子串（与 orchestrator/exporter 措辞对齐）。
_FORMAT_FAIL_MARKERS = (
    "格式未通过",
    "格式未校验",
    "格式校验降级",
    "已降级",
)


@dataclass
class SubmittabilityVerdict:
    """可投递性判定结果。

    Attributes:
        submittable: 是否满足全部硬约束（保守：任一不满足即 False）。
        blockers: 导致不可投递的硬约束原因（人类可读）。
        cautions: 软风险提示（不否决可投递性，但需人工复核）。
    """

    submittable: bool = True
    blockers: list[str] = field(default_factory=list)
    cautions: list[str] = field(default_factory=list)

    @property
    def notes(self) -> list[str]:
        """合并为一份可写入 ExportResult.notes / 展示的说明列表。"""
        out: list[str] = []
        if self.submittable:
            out.append("[可投递性] 通过硬约束检查（仍建议投递前人工终审）。")
        else:
            out.append("[可投递性] 不可直接投递——存在以下硬约束未满足：")
            out.extend(f"  - {b}" for b in self.blockers)
        if self.cautions:
            out.append("[可投递性] 需人工复核的风险：")
            out.extend(f"  - {c}" for c in self.cautions)
        return out


def assess_submittability(
    ws: PaperWorkspace,
    *,
    terminated_reason: str,
    export_notes: list[str] | None = None,
    originality_findings: list[dict] | None = None,
) -> SubmittabilityVerdict:
    """综合判定一份产物是否可直接投递。

    硬约束（任一不满足 → 不可投递）：
    1. GENERATION 模式但无真实研究内容（artifact 缺失或为空）→ 产物为「LLM 推断版」，
       方法/数据可能为编造，绝不可投递。
    2. 反馈循环未以 ``quality_met`` 终止（质量未达标 / 评审不可信 / 停滞 / 预算超额）。
    3. 导出说明含「格式未通过/未校验/降级」——目标格式无法保证正确编译/版式。
    4. 存在空章节（质量报告中的 empty_section 高严重度问题）。

    软风险（记 caution，不否决）：
    - 查重自检发现高文本重合章节。
    """
    verdict = SubmittabilityVerdict()
    export_notes = export_notes or []
    originality_findings = originality_findings or []

    # 硬约束 1：GENERATION 无真实研究内容。
    if ws.input_mode is InputMode.GENERATION and (
        ws.artifact is None or ws.artifact.is_empty()
    ):
        verdict.submittable = False
        verdict.blockers.append(
            "从零生成模式未提供真实研究内容（artifact/领域-问题-方法描述），"
            "产出为 LLM 推断版，方法与数据可能系编造，不可作为真实论文投递。"
        )

    # 硬约束 2：质量未达标。
    if terminated_reason and terminated_reason != "quality_met":
        reason_label = {
            "iteration_limit": "达到迭代上限但质量仍未全维度达标",
            "iteration_limit_unparsed_review": "达到迭代上限且最近评审不可信（解析失败）",
            "stagnation": "连续多轮无实质改进即提前终止，质量未达标",
            "budget_exceeded": "token 预算超额降级终止，质量未达标",
            "deadline_exceeded": "墙钟超时降级终止，质量未达标",
        }.get(terminated_reason, f"反馈循环以非达标原因终止（{terminated_reason}）")
        verdict.submittable = False
        verdict.blockers.append(f"质量闸未通过：{reason_label}。")

    # 硬约束 3：格式未通过/降级。
    fmt_issue = next(
        (
            note
            for note in export_notes
            if any(marker in note for marker in _FORMAT_FAIL_MARKERS)
        ),
        None,
    )
    if fmt_issue is not None:
        verdict.submittable = False
        verdict.blockers.append(
            f"目标格式未通过校验或已降级，无法保证正确编译/版式：{fmt_issue[:300]}"
        )

    # 硬约束 4：空章节。
    empty_sections = [
        i for i in ws.quality_report if i.get("type") == "empty_section"
    ]
    if empty_sections:
        titles = "、".join(
            i.get("message", i.get("section_id", "")) for i in empty_sections[:5]
        )
        verdict.submittable = False
        verdict.blockers.append(f"存在空章节，正文不完整：{titles[:300]}")

    # 软风险：查重高重合。
    for f in originality_findings:
        if f.get("type") == "high_text_overlap":
            verdict.cautions.append(
                f"{f.get('message', '存在文本重合风险')}"
            )

    return verdict


__all__ = ["SubmittabilityVerdict", "assess_submittability"]
