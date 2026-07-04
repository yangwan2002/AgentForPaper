"""护栏闸门（Guardrail Gate）——学术正确性的强制、不可绕过的校验点。

平台的核心安全约束：**任何**改工作区的更新意图，在落盘前必须先经本闸门审查。
闸门对每条 ``ProposedChange`` 逐条判定，产出 ``GateOutcome``（通过者进
``accepted_mutations``，未通过者进 ``rejected`` 并附原因）。落盘由单一写路径
（``apply``）只应用 ``accepted_mutations`` 完成——从而在结构上保证「未过闸门的
内容改动永不落盘」（设计 Property 1）。

两条护栏通道：
- **内容改动**（``CHANGE_CONTENT``）：把该改动 dry-run 应用到工作区副本，跑质量闸
  （及可选的忠实性筛查）；若目标章节出现高严重度问题则拒绝，否则接受。
- **引用增补**（``CHANGE_CITATION``）：逐条核验候选文献的可核验性，**只**接受可核验
  者（由闸门自行合成落盘意图，绝不采用工具原意图，杜绝虚构文献混入）；数量不足
  时产差额说明而非以虚构填充（Req 4.2/4.3）。

依赖倒置：闸门依赖抽象能力（``check`` / ``verify`` / 可选忠实性筛查），具体智能体
（``QualityGate`` / ``CitationVerifier`` / ``CitationFaithfulnessAgent`` 适配器）在
装配阶段注入。任一护栏缺省为 None 时视为「该维度恒通过」，与既有「护栏可选装配」
的向后兼容策略一致。
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from paper_agent.agent_platform.models import (
    CHANGE_CITATION,
    GateOutcome,
    ProposedChange,
    RejectedChange,
)
from paper_agent.workspace.models import PaperWorkspace, ReferenceEntry

# 视为「阻断落盘」的质量问题严重度。
_BLOCKING_SEVERITY = "high"

# 只有「正确性/反幻觉」类问题才阻断落盘；「完整性/风格」类仅作建议（不拦截）。
# 设计定位：护栏守正确性，不管完整性——否则会把「保持原意的润色」反复拦下、
# 逼模型硬塞关键词、白白耗尽迭代次数。
_BLOCKING_ISSUE_TYPES = frozenset({
    "invalid_citation",       # 引用了未核验文献（幻觉）
    "text_citation_invalid",  # 正文标注了未核验文献 id（幻觉）
    "fabricated_metric",      # 正文数字不在真实实验数据中（编造）
    "placeholder",            # TODO/待补充占位（未完成/伪造）
})


@runtime_checkable
class QualityChecker(Protocol):
    """质量闸抽象：对工作区做确定性检查，返回带 ``issues`` 的报告。"""

    def check(self, ws: PaperWorkspace): ...


@runtime_checkable
class ReferenceVerifier(Protocol):
    """引用真实性核验抽象：判断单条文献是否真实可核验。"""

    def verify(self, entry: ReferenceEntry) -> bool: ...


@runtime_checkable
class FaithfulnessScreener(Protocol):
    """忠实性筛查抽象（可选）：返回某章节的无支撑（unsupported）原因列表。

    空列表表示该章节忠实性通过；非空表示存在无支撑声明，应阻断落盘。
    """

    def unsupported_reasons(self, ws: PaperWorkspace, section_id: str) -> list[str]: ...


class GuardrailGate:
    """强制护栏闸门。审查更新意图，决定哪些可落盘（Req 4 / 5）。"""

    def __init__(
        self,
        *,
        quality_gate: QualityChecker | None = None,
        citation_verifier: ReferenceVerifier | None = None,
        faithfulness_screener: FaithfulnessScreener | None = None,
    ) -> None:
        self._quality = quality_gate
        self._verifier = citation_verifier
        self._faithfulness = faithfulness_screener

    def screen(
        self, ws: PaperWorkspace, changes: list[ProposedChange]
    ) -> GateOutcome:
        """审查一批更新意图，产出 ``GateOutcome``（Req 5.1/5.3/5.4、Req 4.2/4.3）。

        划分完备且不重叠：每条 change 要么其（可能被替换的）意图进入
        ``accepted_mutations``，要么进入 ``rejected``（引用增补的差额只产 notes，
        不算 rejected——其可核验子集仍会落盘）。
        """
        accepted: list = []
        rejected: list[RejectedChange] = []
        notes: list[str] = []

        for change in changes:
            if change.kind == CHANGE_CITATION:
                self._screen_citation(change, accepted, notes)
            else:
                self._screen_content(ws, change, accepted, rejected, notes)

        return GateOutcome(
            passed=not rejected,
            accepted_mutations=accepted,
            rejected=rejected,
            notes=notes,
        )

    # --- 内容改动通道 -------------------------------------------------------

    def _screen_content(
        self,
        ws: PaperWorkspace,
        change: ProposedChange,
        accepted: list,
        rejected: list[RejectedChange],
        notes: list[str],
    ) -> None:
        """内容改动：dry-run 到副本 → 质量闸 + 忠实性筛查 → 按目标章节归因。

        仅「正确性/反幻觉」类问题阻断落盘；「完整性/风格」类问题作为建议写入
        ``notes``，不拦截改动（避免反复拦润色、耗尽迭代）。
        """
        preview = self._dry_run(ws, change.mutation)

        blockers, advisories = self._quality_findings(preview, change.section_id)
        blockers.extend(self._faithfulness_blockers(preview, change.section_id))

        if blockers:
            rejected.append(
                RejectedChange(
                    section_id=change.section_id,
                    reason="；".join(blockers),
                    dimension="content",
                )
            )
        else:
            accepted.append(change.mutation)
            # 通过落盘，但把完整性建议如实带出（不拦截）。
            for advice in advisories:
                notes.append(f"（建议·章节 {change.section_id}）{advice}")

    @staticmethod
    def _dry_run(ws: PaperWorkspace, mutation) -> PaperWorkspace:
        """把单个更新意图应用到工作区的**深拷贝**上（不触碰真实工作区）。

        经 ``to_dict``/``from_dict`` 深拷贝，杜绝共享引用被 dry-run 污染。
        """
        preview = PaperWorkspace.from_dict(ws.to_dict())
        mutation(preview)
        return preview

    def _quality_findings(
        self, preview: PaperWorkspace, section_id: str
    ) -> tuple[list[str], list[str]]:
        """质量闸检查后，把目标章节的高严重度问题分为 (阻断项, 建议项)。

        阻断项：正确性/反幻觉类（``_BLOCKING_ISSUE_TYPES``）——拒绝落盘。
        建议项：完整性/风格类——仅提示，不拦截。
        """
        if self._quality is None:
            return [], []
        report = self._quality.check(preview)
        blockers: list[str] = []
        advisories: list[str] = []
        for issue in getattr(report, "issues", []) or []:
            if issue.get("severity") != _BLOCKING_SEVERITY:
                continue
            # section_id 为空的整篇改动 → 归因全部高严重度问题；否则只归因本章节。
            if section_id and issue.get("section_id") not in (section_id, None, ""):
                continue
            message = str(issue.get("message", "质量问题"))
            if issue.get("type") in _BLOCKING_ISSUE_TYPES:
                blockers.append(message)
            else:
                advisories.append(message)
        return blockers, advisories

    def _faithfulness_blockers(
        self, preview: PaperWorkspace, section_id: str
    ) -> list[str]:
        """忠实性筛查后，目标章节的无支撑声明原因。"""
        if self._faithfulness is None:
            return []
        return list(self._faithfulness.unsupported_reasons(preview, section_id))

    # --- 引用增补通道 -------------------------------------------------------

    def _screen_citation(
        self, change: ProposedChange, accepted: list, notes: list[str]
    ) -> None:
        """引用增补：只接受可核验文献，产差额说明；绝不采用工具原意图。"""
        requested = list(change.references)
        verifiable = [ref for ref in requested if self._is_verifiable(ref)]

        if verifiable:
            accepted.append(self._append_references_mutation(verifiable))

        if len(verifiable) < len(requested):
            notes.append(
                f"引用增补：请求 {len(requested)} 条，实际可核验 {len(verifiable)} 条"
                f"（差额 {len(requested) - len(verifiable)} 条不可核验，已略去，未以虚构填充）。"
            )

    def _is_verifiable(self, entry: ReferenceEntry) -> bool:
        """无核验器时保守视为不可核验（fail-closed，杜绝未核验文献落盘）。"""
        if self._verifier is None:
            return False
        return bool(self._verifier.verify(entry))

    @staticmethod
    def _append_references_mutation(refs: list[ReferenceEntry]):
        """合成一个「把可核验文献并入工作区已验证库」的更新意图（去重）。"""

        def _mutate(ws: PaperWorkspace) -> None:
            existing = {r.id for r in ws.verified_references}
            for ref in refs:
                if ref.id in existing:
                    continue
                # 落盘前强制标记 verified=True（已经过核验），保证 Property 4。
                marked = ReferenceEntry(**{**vars(ref), "verified": True})
                ws.verified_references.append(marked)
                existing.add(ref.id)

        return _mutate


__all__ = [
    "GuardrailGate",
    "QualityChecker",
    "ReferenceVerifier",
    "FaithfulnessScreener",
]
