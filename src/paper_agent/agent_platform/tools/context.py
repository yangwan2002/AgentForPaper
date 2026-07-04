"""工具运行期上下文。

``ToolContext`` 聚合平台工具所需的全部运行期依赖，作为工具工厂的单一入参，
避免每个工具重复接线，也便于测试注入 fake。

设计：工作区经 ``session.workspace`` 访问（与会话生命周期一致）；改工作区工具
统一经 ``repo`` + ``gate`` 走 ``apply.commit`` 落盘，绝不直接写 ``session.workspace``。
"""

from __future__ import annotations

from dataclasses import dataclass

from paper_agent.agent_platform.guardrail_gate import GuardrailGate
from paper_agent.agent_platform.models import AgentSession
from paper_agent.elicitation import Elicitor
from paper_agent.workspace.models import PaperWorkspace
from paper_agent.workspace.repository import WorkspaceRepository


@dataclass
class ToolContext:
    """平台工具的共享运行期依赖。"""

    session: AgentSession
    repo: WorkspaceRepository
    gate: GuardrailGate
    elicitor: Elicitor
    output_dir: str = "output"

    @property
    def workspace(self) -> PaperWorkspace:
        """当前会话的工作区（单一真相源）。"""
        return self.session.workspace


__all__ = ["ToolContext"]
