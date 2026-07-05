"""保格式润色的只读审计旁路（inplace-polish-audit）。

为 :class:`~paper_agent.agent_platform.workflows.inplace_polish_workflow.InplacePolishWorkflow`
提供一条**只读、隔离、建议性**的审计：把原稿抽进临时工作区，复用既有的参考文献真实性核验
（``CitationParser`` + ``CitationVerifier``）与引用忠实性核验（``CitationFaithfulnessAgent``
+ ``FaithfulnessJudge``），产出 :class:`AuditReport`。

本模块的设计红线（见 spec）：
- **只读**：只生成报告，绝不改动润色产物、绝不写用户真实工作区。
- **隔离**：审计在临时 ``PaperWorkspace`` 上进行，不经 ``repo``。
- **诚实**：查不到 / grounding 不足 / 检索不可用 → 一律 cannot_verify，绝不假判 supported。
- **故障隔离**：任一步异常都被捕获、如实记入 ``notes``，``audit`` 绝不抛出。

Task 1 实现数据模型与人可读渲染；Task 2 实现 :class:`DraftAuditor`。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # 仅类型标注，避免运行期重导入/循环依赖。
    from paper_agent.agents.citation_faithfulness_agent import CitationFaithfulnessAgent
    from paper_agent.tools.citation import CitationVerifier

# 报告内文本摘录的默认长度上限（脱敏，防止把整段正文回灌）。
_DEFAULT_EXCERPT_MAX = 200

# 忠实性裁决中「算问题、需列出」的取值（supported 不列，cannot_verify 归入提示）。
_FAITHFULNESS_PROBLEM_VERDICTS = ("unsupported", "weak_support")

# 裁决 → 中文标签。
_VERDICT_LABEL = {
    "unsupported": "不支撑",
    "weak_support": "弱支撑",
    "cannot_verify": "无法核验",
    "supported": "支撑",
}


def _truncate(text: str, limit: int) -> str:
    """防御式截断纯字符串（脱敏、有界）。"""
    text = text or ""
    return text if len(text) <= limit else text[:limit]


@dataclass
class ReferenceAuthenticityFinding:
    """单条参考文献的真实性核验结论。"""

    index: int                          # 原文编号（1 起）
    title: str
    verdict: str                        # "real" | "unverifiable" | "retrieval_unavailable"
    note: str = ""                      # 如"年份可能有误：原文2019，真实2020"


@dataclass
class AuditReport:
    """一次只读审计的结构化结果 + 人可读渲染。

    - ``ran``：审计是否实际执行（开关/依赖决定；False 表示未运行）。
    - ``reference_total`` / ``reference_real`` / ``reference_unverifiable``：真实性统计。
    - ``authenticity``：逐条真实性结论。
    - ``faithfulness``：忠实性发现（复用 ``CitationFaithfulnessFinding.to_dict()`` 的 dict）。
    - ``notes``：降级/未完成/异常的诚实说明。
    """

    ran: bool = False
    reference_total: int = 0
    reference_real: int = 0
    reference_unverifiable: int = 0
    authenticity: list[ReferenceAuthenticityFinding] = field(default_factory=list)
    faithfulness: list[dict] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    excerpt_max: int = _DEFAULT_EXCERPT_MAX

    def _faithfulness_problems(self) -> list[dict]:
        """忠实性发现中「不支撑/弱支撑」的条目（需在清单里列出）。"""
        return [
            f for f in self.faithfulness
            if f.get("verdict") in _FAITHFULNESS_PROBLEM_VERDICTS
        ]

    def has_findings(self) -> bool:
        """是否存在需要用户关注的问题（未核验文献 或 不支撑/弱支撑引用）。"""
        return bool(self.reference_unverifiable or self._faithfulness_problems())

    def render(self) -> str:
        """渲染为简洁的人可读问题清单（建议性，脱敏、有界）。"""
        if not self.ran:
            return ""
        limit = self.excerpt_max
        lines: list[str] = ["📋 文献与引用审计（仅供参考的建议，未改动你的润色稿）："]

        # 参考文献真实性。
        if self.reference_total > 0:
            lines.append(
                f"- 参考文献真实性：共 {self.reference_total} 条，"
                f"可核验 {self.reference_real} 条，未核验 {self.reference_unverifiable} 条。"
            )
            for f in self.authenticity:
                if f.verdict == "real" and not f.note:
                    continue  # 干净的真实条目不逐条刷屏
                label = {
                    "real": "可核验",
                    "unverifiable": "未核验/疑似不存在",
                    "retrieval_unavailable": "检索不可用，无法核验",
                }.get(f.verdict, f.verdict)
                title = _truncate(f.title, limit)
                extra = f"（{_truncate(f.note, limit)}）" if f.note else ""
                lines.append(f"  · [{f.index}] {title}：{label}{extra}")
        else:
            lines.append("- 参考文献真实性：未发现可解析的参考文献表。")

        # 引用忠实性。
        problems = self._faithfulness_problems()
        if problems:
            lines.append(f"- 引用忠实性：发现 {len(problems)} 处可能不支撑/弱支撑：")
            for f in problems:
                verdict = _VERDICT_LABEL.get(f.get("verdict"), f.get("verdict", ""))
                excerpt = _truncate(f.get("claim_excerpt", ""), limit)
                rid = f.get("cited_reference_id", "")
                sec = f.get("section_id", "")
                rationale = _truncate(f.get("rationale", ""), limit)
                lines.append(
                    f"  · 章节 {sec} 引用 [{rid}]（{verdict}）：{excerpt}"
                    + (f" —— {rationale}" if rationale else "")
                )
        elif self.faithfulness:
            lines.append("- 引用忠实性：已核验引用中未发现明显不支撑。")

        # 无任何问题时的明确文案。
        if not self.has_findings():
            lines.append("- 结论：参考文献均可核验、引用未发现明显不支撑。")

        # 诚实降级说明。
        for note in self.notes:
            lines.append(f"- 说明：{_truncate(note, limit)}")

        return "\n".join(lines)


class DraftAuditor:
    """只读、隔离、故障隔离的原稿审计器（inplace-polish-audit · Task 2）。

    把原稿抽进**临时** ``PaperWorkspace``（不经 repo），复用既有件做两项核验：
    参考文献真实性（``CitationVerifier``）与引用忠实性（``CitationFaithfulnessAgent``）。
    产出 :class:`AuditReport`。

    依赖注入（依赖倒置，便于测试）：
    - ``verifier``：``CitationVerifier``，按标题/DOI 回查真实性。
    - ``faithfulness_agent``：预构造的 ``CitationFaithfulnessAgent``（读工作区自 ctx，
      与工作区无关，可跨稿复用）；``None`` 表示判定器不可用（如 mock）→ 跳过忠实性。
    - ``retrieval_available``：检索是否可用；False → 真实性全标 retrieval_unavailable、
      不打网络（Req 3.4 / 7.2）。

    契约：``audit`` **只读**（不改原稿文件、不写用户工作区）、**绝不抛出**（任一步异常
    捕获后记 notes 继续，Req 6.1）。
    """

    def __init__(
        self,
        verifier: "CitationVerifier",
        faithfulness_agent: "CitationFaithfulnessAgent | None" = None,
        *,
        retrieval_available: bool = True,
        excerpt_max: int = _DEFAULT_EXCERPT_MAX,
    ) -> None:
        self._verifier = verifier
        self._faithfulness_agent = faithfulness_agent
        self._retrieval_available = bool(retrieval_available)
        self._excerpt_max = int(excerpt_max)

    def audit(self, source_path: str) -> AuditReport:
        """只读审计一个原稿文件；全程异常隔离，返回 :class:`AuditReport`（绝不抛出）。"""
        report = AuditReport(ran=True, excerpt_max=self._excerpt_max)
        try:
            text, triples = self._load(source_path)
        except Exception as exc:  # noqa: BLE001 - 解析失败不抛，如实记 notes
            report.notes.append(f"无法解析原稿，未能审计（{type(exc).__name__}）")
            return report
        if not (text or "").strip() or not triples:
            report.notes.append("原稿无可解析内容，未能审计")
            return report

        temp_ws = self._build_temp_workspace(text, triples)

        # 真实性核验（结果里的真实条目并入临时工作区，供忠实性 grounding 用）。
        try:
            self._audit_authenticity(text, temp_ws, report)
        except Exception as exc:  # noqa: BLE001 - 真实性异常不连累其余
            report.notes.append(f"参考文献真实性核验异常（{type(exc).__name__}）")

        # 忠实性核验（复用 CitationFaithfulnessAgent；判定器不可用则跳过）。
        try:
            self._audit_faithfulness(temp_ws, report)
        except Exception as exc:  # noqa: BLE001 - 忠实性异常不连累产物/其余
            report.notes.append(f"引用忠实性核验异常（{type(exc).__name__}）")

        return report

    # --- 子步骤 ---------------------------------------------------------- #

    @staticmethod
    def _load(source_path: str):
        """抽取原稿全文 + 章节三元组（复用 import_draft 的 load_sections）。"""
        from paper_agent.agent_platform.tools.import_draft import load_sections

        return load_sections(source_path)

    @staticmethod
    def _build_temp_workspace(text: str, triples):
        """构造临时、隔离的审计工作区（内存对象，不经 repo）。"""
        from paper_agent.workspace.models import (
            InputMode,
            PaperWorkspace,
            SectionDraft,
        )

        ws = PaperWorkspace(
            workspace_id="__audit_ephemeral__", input_mode=InputMode.DRAFT_REVISION
        )
        ws.original_draft = text
        ws.section_drafts = {
            sid: SectionDraft(section_id=sid, title=title, content=content)
            for sid, title, content in triples
        }
        return ws

    def _audit_authenticity(self, text: str, temp_ws, report: AuditReport) -> None:
        """解析参考文献表并逐条核验真实性；真实条目并入临时工作区。"""
        from paper_agent.tools.citation_parser import CitationParser
        from paper_agent.workspace.models import ReferenceEntry

        parsed = CitationParser().parse(text)
        report.reference_total = len(parsed.references)
        if not parsed.references:
            report.notes.append("未发现可解析的参考文献表")
            return

        if not self._retrieval_available:
            for i, ref in enumerate(parsed.references, start=1):
                report.authenticity.append(
                    ReferenceAuthenticityFinding(
                        i, ref.title or "", "retrieval_unavailable"
                    )
                )
            report.reference_unverifiable = report.reference_total
            report.notes.append("检索不可用，参考文献真实性未能核验")
            return

        verified_entries: list = []
        for i, ref in enumerate(parsed.references, start=1):
            try:
                result = self._verifier.verify_by_metadata(ref)
            except Exception:  # noqa: BLE001 - 单条核验失败按不可核验处理
                result = None
            if result is not None and getattr(result, "exists", False):
                report.reference_real += 1
                report.authenticity.append(
                    ReferenceAuthenticityFinding(
                        i, ref.title or "", "real", getattr(result, "note", "") or ""
                    )
                )
                verified_entries.append(
                    self._build_verified_entry(i, ref, result.matched, ReferenceEntry)
                )
            else:
                report.reference_unverifiable += 1
                note = getattr(result, "note", "") if result is not None else ""
                report.authenticity.append(
                    ReferenceAuthenticityFinding(i, ref.title or "", "unverifiable", note)
                )

        # 真实条目并入临时工作区（保留原文编号作 id），仅供忠实性 grounding 使用。
        temp_ws.verified_references = verified_entries

    @staticmethod
    def _build_verified_entry(index: int, ref, matched, ReferenceEntry):
        """据核验结果构造入库条目：保留原文编号作 id，元数据优先取真实记录。"""
        meta = matched if matched is not None else ref
        return ReferenceEntry(
            id=str(index),
            title=getattr(meta, "title", "") or "",
            authors=list(getattr(meta, "authors", []) or []),
            year=getattr(meta, "year", None),
            source_id=getattr(meta, "source_id", "") or getattr(ref, "source_id", ""),
            source=getattr(meta, "source", "") or "draft",
            verified=True,
        )

    def _audit_faithfulness(self, temp_ws, report: AuditReport) -> None:
        """在临时工作区上跑忠实性核验（判定器不可用则跳过并记 notes）。"""
        if self._faithfulness_agent is None:
            report.notes.append("判定器不可用，未做引用忠实性核验")
            return
        from paper_agent.agents.base import AgentContext

        ctx = AgentContext(workspace=temp_ws)
        result = self._faithfulness_agent.run(ctx)
        # CitationFaithfulnessAgent 经 mutations 写 ws.citation_faithfulness（替换）。
        for mutate in result.mutations:
            mutate(temp_ws)
        report.faithfulness = list(temp_ws.citation_faithfulness)


__all__ = ["ReferenceAuthenticityFinding", "AuditReport", "DraftAuditor"]
