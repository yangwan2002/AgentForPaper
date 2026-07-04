"""确定性工作流抽象（intent-routing-and-workflows · Task 3）。

工作流把"固定流程任务"从自由智能体手里拿走：工具/步骤/参数**写死在代码里**，顶层 LLM
不参与编排（Property 4）。每个工作流对应一个固定意图（:class:`~paper_agent.agent_platform.routing.Intent`），
复用既有的确定性能力（如 ``convert_document_core`` / inplace 润色），只负责"按固定序编排 +
诚实上报"。

契约：
- 产物写**新文件**、原稿只读（Property 5）。
- 任一步失败 → ``WorkflowResult(ok=False, unresolved=[...])`` 诚实上报，绝不降级重建（Property 6）。
- 任何改工作区的步骤仍经既有护栏/单一写路径（Property 7）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

from paper_agent.agent_platform.routing import Intent
from paper_agent.agent_platform.tools.context import ToolContext


@dataclass
class WorkflowResult:
    """一次工作流执行的结构化结果。

    Attributes:
        ok: 是否整体成功。
        files: 产出的新文件路径（原稿不在其中）。
        notes: 可读的执行说明片段。
        unresolved: 失败/未达成项（失败时非空，诚实上报）。
    """

    ok: bool
    files: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    unresolved: list[str] = field(default_factory=list)

    def message(self) -> str:
        """人可读结论：成功给 notes 拼接，失败额外附 unresolved。"""
        body = "".join(self.notes)
        if not self.ok and self.unresolved:
            reasons = "；".join(self.unresolved)
            return (body + f"\n未完成：{reasons}").strip()
        return body


@runtime_checkable
class Workflow(Protocol):
    """确定性工作流协议：绑定一个固定意图，按写死步骤执行并返回结构化结果。"""

    intent: Intent

    def run(self, ctx: ToolContext, params: dict) -> WorkflowResult:
        """按固定步骤执行任务；不由 LLM 决定工具序列。"""
        ...


__all__ = ["WorkflowResult", "Workflow"]
