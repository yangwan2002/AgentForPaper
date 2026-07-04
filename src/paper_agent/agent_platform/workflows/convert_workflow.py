"""ConvertWorkflow：跨格式转换的确定性工作流（intent-routing-and-workflows · Task 3）。

固定步骤（写死、不经 LLM 编排）= 解析源 → pandoc 直转（保公式）→ 修表格列宽 → 三线表 →
双栏（按 params）→ 套排版。全部委托给已实现且确定性的 ``convert_document_core``，工作流只
把它按固定序编排、并把结果翻译成 :class:`WorkflowResult`（含失败诚实上报）。

参数（来自 :class:`~paper_agent.agent_platform.routing.RouteDecision` 的 ``params``）：
- ``to_format``：目标格式（缺省 ``docx``）。
- ``source_path``：源文件绝对路径（缺省用会话已导入的原文件）。
- ``two_column``：docx 是否双栏（缺省 False）。
- ``three_line_table``：docx 表格是否套三线表（缺省 True）。
"""

from __future__ import annotations

from paper_agent.agent_platform.routing import Intent
from paper_agent.agent_platform.tools.context import ToolContext
from paper_agent.agent_platform.tools.convert_tool import convert_document_core
from paper_agent.agent_platform.workflows.base import WorkflowResult


class ConvertWorkflow:
    """跨格式转换工作流（``Intent.CONVERT_FORMAT``）。"""

    intent = Intent.CONVERT_FORMAT

    def run(self, ctx: ToolContext, params: dict) -> WorkflowResult:
        params = params or {}
        to_format = str(params.get("to_format") or "docx").lower()
        source_path = params.get("source_path")
        two_column = bool(params.get("two_column", False))
        three_line_table = bool(params.get("three_line_table", True))

        outcome = convert_document_core(
            ctx,
            to_format=to_format,
            path=source_path,
            two_column=two_column,
            three_line_table=three_line_table,
        )
        if not outcome.ok:
            # 诚实上报：不降级到"导入重建"这类会丢公式/乱结构的路径。
            return WorkflowResult(
                ok=False, unresolved=[outcome.error or "转换失败"]
            )
        return WorkflowResult(ok=True, files=list(outcome.files), notes=list(outcome.notes))


__all__ = ["ConvertWorkflow"]
