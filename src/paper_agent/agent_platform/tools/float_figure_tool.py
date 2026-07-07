"""float_figure 工具：把 docx 里某张图设为浮动、页顶、跨栏满宽、上下环绕（对标 figure*[t]）。

大模型负责"理解要动哪张图"（把用户说的"图1"映射成第 N 张内联图），真正改 XML 的动作由
确定性函数 :func:`~paper_agent.export.docx_float.float_figure_top` 完成（写死、和手动等价）。

诚实边界：产出效果 = 手动在 Word 里摆浮动图的效果。"锚到所在页页顶"确定；要"落到下一页
顶部"用 ``force_next_page``（上一页底可能留白）。精确落页 + 无留白需配合视觉验收闭环反馈微调。
"""

from __future__ import annotations

import os

from paper_agent.agent_platform.tools.context import ToolContext
from paper_agent.export.docx_float import float_figure_top
from paper_agent.tools.registry import ToolRegistry

_SCHEMA = {
    "type": "object",
    "properties": {
        "index": {
            "type": "integer",
            "minimum": 1,
            "description": "要处理的是第几张图（按文档顺序，从 1 开始；用户说'图1'即 1）。",
        },
        "path": {
            "type": "string",
            "description": "docx 路径；省略则用本会话记忆里最近的产物（last_output_path）。",
        },
        "span_columns": {
            "type": "boolean",
            "description": "是否放大到版心整宽（跨双栏满宽）。默认 true。",
        },
        "force_next_page": {
            "type": "boolean",
            "description": (
                "是否强制图从下一页顶部开始（前置分页符）。默认 false=锚到所在页页顶。"
                "注意：force 会让上一页底部可能留白。"
            ),
        },
    },
    "required": ["index"],
}

_DESCRIPTION = (
    "把 docx 里指定的一张图设为**浮动 + 页顶对齐 + 跨栏满宽 + 文字上下环绕**（对标 LaTeX "
    "figure* 顶部浮动效果）。当用户要求'把图放到页顶/跨双栏/浮起来'时用它。它锚定到图所在页的"
    "页顶；若要落到下一页顶部，设 force_next_page=true（代价：上一页底可能留白）。只改这一张图的"
    "浮动属性，不动正文/公式/其它结构。"
)


def _resolve_docx(ctx: ToolContext, path: str | None) -> str:
    candidate = (path or "").strip().strip('"').strip("'")
    if candidate:
        return candidate
    profile = getattr(ctx.workspace, "profile", None) or {}
    return str(profile.get("last_output_path", "") or "")


def _handle_float_figure(
    ctx: ToolContext,
    index: int,
    path: str | None = None,
    span_columns: bool = True,
    force_next_page: bool = False,
) -> str:
    docx_path = _resolve_docx(ctx, path)
    if not docx_path or not os.path.isfile(docx_path):
        return "未找到 docx 文件：请提供 path，或先转换/导出一个 docx 产物。"
    if not docx_path.lower().endswith(".docx"):
        return "float_figure 只作用于 .docx 文件。"
    ok, msg = float_figure_top(
        docx_path, index=int(index),
        span_columns=bool(span_columns), force_next_page=bool(force_next_page),
    )
    ctx.session.record(
        "float_figure", index=int(index), ok=ok,
        span_columns=bool(span_columns), force_next_page=bool(force_next_page),
        files=[docx_path] if ok else [],
    )
    return msg


def register_float_figure(registry: ToolRegistry, ctx: ToolContext) -> None:
    """注册 float_figure 工具。"""
    registry.register(
        name="float_figure",
        description=_DESCRIPTION,
        handler=lambda index, path=None, span_columns=True, force_next_page=False: (
            _handle_float_figure(ctx, index, path, span_columns, force_next_page)
        ),
        parameters=_SCHEMA,
    )


__all__ = ["register_float_figure"]
