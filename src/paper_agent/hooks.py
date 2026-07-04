"""可插拔的扩展点（hooks）。

借鉴通用 agent（Claude Code 的 PreToolUse/PostToolUse 等）的钩子思想：在智能体
执行与工具调用的前后暴露扩展点，使外部可注入审计 / 限流 / 缓存 / 自动校验等
策略，而无需改动核心流程。

与 ``EventSink`` 的区别：``EventSink`` 是**单向只读观测**（业务发事件、订阅方
自行呈现）；``Hooks`` 是**可干预的扩展点**（外部可在动作前后插入逻辑）。两者
互补：sink 负责"看见"，hooks 负责"插手"（#15 修复）。

默认实现为全空操作；子类化或直接替换方法即可定制。
"""

from __future__ import annotations

from typing import Any


class Hooks:
    """扩展点基类。所有方法默认空操作；子类化以定制。"""

    def before_agent(self, name: str, ctx: Any) -> None:
        """智能体执行前（可读 ctx.workspace，不应修改）。"""

    def after_agent(self, name: str, ctx: Any, result: Any) -> None:
        """智能体执行后（可读 result.mutations/logs/payload，不应修改）。"""

    def before_tool_call(self, name: str, arguments: dict) -> None:
        """工具执行前（可记录/限流/改写参数副本）。"""

    def after_tool_call(
        self, name: str, arguments: dict, result: Any, error: "BaseException | None"
    ) -> None:
        """工具执行后（含异常时 error 非空；可审计结果/缓存）。"""


__all__ = ["Hooks"]
