"""智能体统一接口。

所有智能体实现同一 `Agent` 接口，使 Orchestrator 可一致调度，
并便于在骨架阶段替换为桩实现。

关键设计：智能体**不直接写工作区**。它接收一个只读的 `AgentContext`，
返回一个 `AgentResult`，其中 `mutate` 是对工作区的「更新意图」，
由 Orchestrator 经仓储统一原子落盘（保证持久化一致性，Property 3）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Protocol, runtime_checkable

from paper_agent.workspace.models import PaperWorkspace

# 对工作区的原地修改函数。
WorkspaceMutation = Callable[[PaperWorkspace], None]


@dataclass
class AgentContext:
    """智能体运行的只读输入上下文。

    workspace 仅供读取；任何修改都应通过 AgentResult.mutations 返回。
    extras 用于传递任务相关的临时参数（如当前章节 id、评审建议等）。
    """

    workspace: PaperWorkspace
    extras: dict = field(default_factory=dict)


@dataclass
class AgentResult:
    """智能体产出。

    mutations: 对工作区的更新意图列表，由 Orchestrator 依次原子应用。
    logs: 供观测/调试的日志条目。
    payload: 返回给 Orchestrator 的非持久化数据（如评审给出的待修订章节）。
    """

    mutations: list[WorkspaceMutation] = field(default_factory=list)
    logs: list[str] = field(default_factory=list)
    payload: dict = field(default_factory=dict)


@runtime_checkable
class Agent(Protocol):
    name: str

    def run(self, ctx: AgentContext) -> AgentResult:
        ...
