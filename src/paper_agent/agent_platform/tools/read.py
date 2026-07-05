"""read_section 工具：读取某章节当前正文（只读，支持分页）。

改写/润色类任务的前置——顶层 LLM 需要先看到章节现状，才能产出改写文本再经
写工具落盘。只读：不产生 ``ProposedChange``。

**分页读取**：超长章节的整段正文会被 agent 循环按 token 截断（``max_tool_result_tokens``），
导致尾部内容永远读不到。为此本工具支持 ``offset`` / ``limit`` 字符窗口：从 ``offset``
起返回至多 ``limit`` 个字符，并在末尾附「还有 N 字符未读、下次用 offset=X 续读」的提示，
让 LLM 能像翻页一样读完整章。不传则从头读默认窗口。
"""

from __future__ import annotations

from paper_agent.agent_platform.tools.context import ToolContext
from paper_agent.tools.registry import ToolRegistry

# 单次分页默认字符窗口。取值须保证「本页正文 + 续读提示」在下游 agent 循环的
# ``max_tool_result_tokens``（默认 2000）之内，否则页尾的续读提示会被 token 截断吃掉、
# LLM 就看不到 next offset 而无法翻页。中文近似 1 字符≈1 token，故默认取 1500 字符
# 留足页眉/页脚与余量；需要更大窗口可显式传 limit（但过大仍可能被 token 截断）。
_DEFAULT_LIMIT = 1500
# limit 合法上限（防一次拉过大又被 token 截断，失去分页意义）。
_MAX_LIMIT = 20000

_READ_SCHEMA = {
    "type": "object",
    "properties": {
        "section_id": {"type": "string", "description": "目标章节 id"},
        "offset": {
            "type": "integer",
            "minimum": 0,
            "description": (
                "起始字符位置（0 起）。读超长章节时用它翻页：上次返回提示的"
                " next offset 填这里续读。默认 0（从头读）。"
            ),
        },
        "limit": {
            "type": "integer",
            "minimum": 1,
            "description": (
                f"本次最多返回的字符数（默认 {_DEFAULT_LIMIT}，上限 {_MAX_LIMIT}）。"
            ),
        },
    },
    "required": ["section_id"],
}

_READ_DESCRIPTION = (
    "读取指定章节的当前正文内容。改写或润色章节前，先用此工具查看现状。此工具只读，"
    "不修改论文。章节很长时会分页返回：末尾会提示「还有 N 字符未读」及续读用的 offset，"
    "按提示再次调用即可读完整章（不要漏读尾部）。"
)


def _clamp_limit(limit: int | None) -> int:
    """把 limit 规整到 [1, _MAX_LIMIT]；未给用默认。"""
    if limit is None:
        return _DEFAULT_LIMIT
    try:
        value = int(limit)
    except (TypeError, ValueError):
        return _DEFAULT_LIMIT
    return max(1, min(_MAX_LIMIT, value))


def _clamp_offset(offset: int | None) -> int:
    """把 offset 规整到非负整数；非法视为 0。"""
    try:
        return max(0, int(offset)) if offset is not None else 0
    except (TypeError, ValueError):
        return 0


def _handle_read(
    ctx: ToolContext, section_id: str, offset: int | None = None, limit: int | None = None
) -> str:
    draft = ctx.workspace.section_drafts.get(section_id)
    if draft is None:
        return f"章节 {section_id!r} 不存在。可先用 locate_section 定位正确的章节 id。"

    body = draft.content or ""
    total = len(body)
    start = _clamp_offset(offset)
    window = _clamp_limit(limit)

    ctx.session.record("read_section", section_id=section_id, offset=start, limit=window)

    if total == 0:
        return f"章节 id={section_id}《{draft.title}》当前正文：\n（空）"

    if start >= total:
        return (
            f"章节 id={section_id}《{draft.title}》共 {total} 字符，"
            f"offset={start} 已超出末尾，无更多内容。"
        )

    end = min(start + window, total)
    chunk = body[start:end]
    remaining = total - end

    header = (
        f"章节 id={section_id}《{draft.title}》正文"
        f"（第 {start}–{end} 字符，共 {total} 字符）："
    )
    footer = ""
    if remaining > 0:
        footer = (
            f"\n\n[还有 {remaining} 字符未读——续读请再次调用 read_section，"
            f"参数 offset={end}]"
        )
    return f"{header}\n{chunk}{footer}"


def register_read_section(registry: ToolRegistry, ctx: ToolContext) -> None:
    registry.register(
        name="read_section",
        description=_READ_DESCRIPTION,
        handler=lambda section_id, offset=None, limit=None: _handle_read(
            ctx, section_id, offset, limit
        ),
        parameters=_READ_SCHEMA,
    )


__all__ = ["register_read_section"]
