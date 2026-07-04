"""模型可调用的质量闸 / 引用检查只读工具（升级 Req 6.7 / 6.8）。

为模型在工具循环中提供两类「客观验证」能力，复用既有确定性组件，
仅做模型可调用封装：

- ``run_quality_gate``：复用既有 :class:`QualityGate`，触发确定性质量闸检查，
  返回问题清单（可为空）。只读，不变更工作区（Req 6.7）。
- ``check_citations``：校验正文（各章节草稿）引用的文献 id 是否都在
  已验证文献库中，返回未通过校验的引用 id 清单（可为空）。只读，
  不变更工作区（Req 6.8）。

设计要点：
- 两个工具均为**只读**：仅读取工作区状态做判断，绝不写回任何字段
  （保持「智能体不直接写工作区」的契约）。``QualityGate.check`` 本身
  也是纯读取，不产生副作用。
- 返回字符串结果，直接适配 LLM function calling 工具循环（结果会被字符串化回填）。
- ``check_citations`` 以「引用 id 是否属于已验证库」的成员关系判断为核心语义
  （Req 6.8）。这是确定性的工作区内检查；可选注入 :class:`CitationVerifier`
  以保留引用真实性硬约束的语义关联，但不触发任何工作区变更。
"""

from __future__ import annotations

from paper_agent.tools.citation import CitationVerifier
from paper_agent.tools.quality_gate import QualityGate
from paper_agent.workspace.models import PaperWorkspace

_NO_PARAMS_SCHEMA = {"type": "object", "properties": {}}


class QualityCheckTools:
    """质量闸与引用检查的只读封装，供模型按需触发客观验证。

    持有工作区引用以反映其最新状态，但所有方法均只读，不变更工作区。
    """

    def __init__(
        self,
        workspace: PaperWorkspace,
        gate: QualityGate | None = None,
        verifier: CitationVerifier | None = None,
    ) -> None:
        # 仅持有引用用于读取最新状态；下列方法不会修改工作区。
        self._ws = workspace
        self._gate = gate or QualityGate()
        # 可选：保留与引用真实性核验器的语义关联（当前成员关系检查不依赖它）。
        self._verifier = verifier

    def run_quality_gate(self) -> str:
        """触发确定性质量闸检查，返回问题清单（可为空）。只读（Req 6.7）。

        复用既有 :class:`QualityGate`，``check`` 为纯读取，不变更工作区。
        """
        report = self._gate.check(self._ws)
        issues = report.issues
        if not issues:
            return "质量闸检查通过：未发现问题。"

        lines = [
            f"质量闸发现 {len(issues)} 个问题"
            f"（高严重度 {len(report.high_issues)} 个，{'未通过' if not report.passed else '可放行'}）："
        ]
        for idx, issue in enumerate(issues, start=1):
            severity = issue.get("severity", "unknown")
            itype = issue.get("type", "unknown")
            section_id = issue.get("section_id")
            section_part = f" [section_id={section_id}]" if section_id else ""
            message = issue.get("message", "")
            lines.append(f"{idx}. ({severity}/{itype}){section_part} {message}")
        return "\n".join(lines)

    def check_citations(self) -> str:
        """校验正文引用 id 是否都在已验证库中，返回未通过的 id 清单（可为空）。

        只读（Req 6.8）：仅读取章节草稿的 ``cited_reference_ids`` 与工作区的
        已验证文献库做成员关系判断，不变更工作区。
        """
        verified_ids = self._ws.verified_reference_ids()

        # 收集所有「引用了未经核验/不存在文献」的 id，并记录引用它的章节。
        failing: dict[str, list[str]] = {}
        for node in self._ws.ordered_sections():
            draft = self._ws.section_drafts.get(node.section_id)
            if draft is None:
                continue
            for rid in draft.cited_reference_ids:
                if rid not in verified_ids:
                    failing.setdefault(rid, []).append(node.section_id)

        # 同时覆盖不在大纲中的章节草稿（防御式，确保不遗漏）。
        ordered_ids = {n.section_id for n in self._ws.outline}
        for section_id, draft in self._ws.section_drafts.items():
            if section_id in ordered_ids:
                continue
            for rid in draft.cited_reference_ids:
                if rid not in verified_ids:
                    failing.setdefault(rid, []).append(section_id)

        if not failing:
            return "引用检查通过：正文引用的文献 id 均在已验证库中。"

        lines = [f"发现 {len(failing)} 个未通过校验的引用 id（不在已验证库中）："]
        for rid in sorted(failing):
            sections = ", ".join(sorted(set(failing[rid])))
            lines.append(f"- {rid}（被章节引用：{sections}）")
        return "\n".join(lines)
