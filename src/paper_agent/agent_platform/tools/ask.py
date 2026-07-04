"""ask_user 工具：Agent_Loop 按需向作者澄清（Req 8.1）。

复用既有 ``tools/ask_user_tool.AskUserTool``（配额 + 缓存 + 非交互守卫）。收集到的
问答累积在 ``AskUserTool`` 实例中，由 TaskAgent 在会话收尾时经单一写路径持久化到
``ws.profile['clarification_answers']``（本工具本身不写工作区，Req 6.1）。

非交互模式下（``AutoElicitor``）该工具返回「作者不可用」提示，Agent_Loop 据此自行
合理处理或取默认，符合优雅降级（Req 8.4）。
"""

from __future__ import annotations

from paper_agent.agent_platform.tools.context import ToolContext
from paper_agent.tools.ask_user_tool import AskUserTool, register_ask_user_tool
from paper_agent.tools.registry import ToolRegistry


def build_ask_user_tool(ctx: ToolContext, *, budget: int = 3) -> AskUserTool:
    """据上下文构造 AskUserTool，种子为工作区已持久化的澄清答案（续跑回放）。"""
    existing = list(ctx.workspace.profile.get("clarification_answers") or [])
    return AskUserTool(ctx.elicitor, existing_answers=existing, budget=budget)


def register_ask_user(
    registry: ToolRegistry, ctx: ToolContext, *, budget: int = 3
) -> AskUserTool:
    """构造并注册 ask_user 工具，返回工具实例（供 TaskAgent 收尾持久化问答）。"""
    tool = build_ask_user_tool(ctx, budget=budget)
    register_ask_user_tool(registry, tool)
    return tool


__all__ = ["build_ask_user_tool", "register_ask_user"]
