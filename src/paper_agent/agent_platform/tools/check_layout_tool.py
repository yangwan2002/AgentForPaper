"""check_layout 工具（visual-layout-acceptance · Task 9）。

让主智能体在**自认为需要**时主动请求一次视觉版面校验（除确定性触发外的补充入口）。

本工具**不自己**渲染/看图——它只在 transcript 里留一条 ``check_layout`` 记录；真正的
渲染 + 多模态判断 + 有界重改由 ``ChatController`` 收尾统一编排（它据本轮是否出现
``check_layout`` 记录、或是否含版面相关操作来触发 :class:`VisualAcceptanceGate`）。
这样保证：确定性触发（安全网）与 agent 主动请求走**同一条**受控编排，主智能体既能
主动要校验、也**无法**靠不调本工具来跳过对其自身版面改动的校验。

仅在视觉验收主开关开启时才注册（关闭时不暴露给模型，行为不变）。
"""

from __future__ import annotations

from paper_agent.agent_platform.tools.context import ToolContext
from paper_agent.tools.registry import ToolRegistry

_SCHEMA = {
    "type": "object",
    "properties": {
        "reason": {
            "type": "string",
            "description": "为什么想校验版面（可选，便于日志/回溯）。",
        }
    },
    "required": [],
}

_DESCRIPTION = (
    "请求对本轮产出的 docx 做一次**视觉版面校验**（把文档渲染成图、用视觉模型判断"
    "版面是否符合用户诉求，不达标会有界重改）。当你刚做了影响版面的改动（图跨栏 / 分栏 / "
    "字体字号 / 表格 / 转 docx 等）、想确认版面确实对了时调用它。"
    "注意：它不立即返回判定，实际校验在本轮收尾统一进行；即便你不调用，涉及版面的改动"
    "也会被自动校验。"
)


def _handle_check_layout(ctx: ToolContext, reason: str = "") -> str:
    ctx.session.record("check_layout", reason=str(reason or ""))
    return "已登记视觉版面校验请求；将在本轮收尾时对 docx 产物渲染看图并按需有界重改。"


def register_check_layout(registry: ToolRegistry, ctx: ToolContext) -> None:
    """注册 check_layout 工具（仅在视觉验收启用时调用本函数）。"""
    registry.register(
        name="check_layout",
        description=_DESCRIPTION,
        handler=lambda reason="": _handle_check_layout(ctx, reason),
        parameters=_SCHEMA,
    )


__all__ = ["register_check_layout"]
