"""read_section 工具：读取某章节当前正文（只读）。

改写/润色类任务的前置——顶层 LLM 需要先看到章节现状，才能产出改写文本再经
写工具落盘。只读：不产生 ``ProposedChange``。
"""

from __future__ import annotations

from paper_agent.agent_platform.tools.context import ToolContext
from paper_agent.tools.registry import ToolRegistry

_READ_SCHEMA = {
    "type": "object",
    "properties": {
        "section_id": {"type": "string", "description": "目标章节 id"},
    },
    "required": ["section_id"],
}

_READ_DESCRIPTION = (
    "读取指定章节的当前正文内容。改写或润色章节前，先用此工具查看现状。"
    "此工具只读，不修改论文。"
)


def _handle_read(ctx: ToolContext, section_id: str) -> str:
    draft = ctx.workspace.section_drafts.get(section_id)
    if draft is None:
        return f"章节 {section_id!r} 不存在。可先用 locate_section 定位正确的章节 id。"
    ctx.session.record("read_section", section_id=section_id)
    body = draft.content or "（空）"
    return f"章节 id={section_id}《{draft.title}》当前正文：\n{body}"


def register_read_section(registry: ToolRegistry, ctx: ToolContext) -> None:
    registry.register(
        name="read_section",
        description=_READ_DESCRIPTION,
        handler=lambda section_id: _handle_read(ctx, section_id),
        parameters=_READ_SCHEMA,
    )


__all__ = ["register_read_section"]
