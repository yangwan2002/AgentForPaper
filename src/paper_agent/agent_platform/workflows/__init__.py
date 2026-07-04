"""确定性工作流层：固定流程任务的写死编排（不经 LLM 工具选择）。

- :class:`Workflow` / :class:`WorkflowResult`：工作流协议与结构化结果。
- :class:`ConvertWorkflow`：跨格式转换（复用 ``convert_document_core``）。
"""

from __future__ import annotations

from paper_agent.agent_platform.workflows.base import Workflow, WorkflowResult
from paper_agent.agent_platform.workflows.convert_workflow import ConvertWorkflow
from paper_agent.agent_platform.workflows.inplace_polish_workflow import (
    InplacePolishWorkflow,
)

__all__ = ["Workflow", "WorkflowResult", "ConvertWorkflow", "InplacePolishWorkflow"]
