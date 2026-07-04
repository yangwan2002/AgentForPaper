"""locate_section 工具：把用户对章节的自然语言指代定位到具体章节（只读）。

支持三级匹配（越精确越优先）：
1. ``section_id`` 精确匹配（小写）；
2. ``title`` 子串匹配（小写）；
3. 按体裁推断——把指代（如「实验」「引言」）推断为 ``SectionType``，匹配同体裁章节。

命中唯一 → 返回该章节 id/title；命中多个 → 返回「需澄清」信号与候选列表，供
Agent_Loop 决定调 ``ask_user`` 向作者确认（Req 3.3）；命中为空 → 返回「未找到」。

只读：不产生任何 ``ProposedChange``，不改工作区。
"""

from __future__ import annotations

from paper_agent.agent_platform.tools.context import ToolContext
from paper_agent.prompts.section_types import SectionType, infer_section_type
from paper_agent.tools.registry import ToolRegistry
from paper_agent.workspace.models import OutlineNode, PaperWorkspace

_LOCATE_SCHEMA = {
    "type": "object",
    "properties": {
        "reference": {
            "type": "string",
            "description": "用户对目标章节的指代，可为章节标题、章节 id 或体裁词"
            "（如「实验」「引言」「相关工作」）。",
        }
    },
    "required": ["reference"],
}

_LOCATE_DESCRIPTION = (
    "在当前论文中定位用户指代的章节。返回唯一命中的章节 id 与标题；若有多个候选，"
    "会提示需要澄清并列出候选——此时应调用 ask_user 让作者确认，不要擅自选择；"
    "若找不到则如实返回未找到。此工具只读，不修改论文。"
)


def find_section_matches(
    ws: PaperWorkspace, reference: str
) -> list[OutlineNode]:
    """按三级规则返回匹配的章节节点（保持大纲顺序，去重）。

    纯函数，便于单测：先 id 精确，再 title 子串，最后体裁推断；一旦某级有命中即
    返回该级结果（更精确的优先，不与低优先级混合）。
    """
    ref = (reference or "").strip().lower()
    if not ref:
        return []

    sections = ws.ordered_sections()

    # 1) section_id 精确。
    exact = [n for n in sections if n.section_id.lower() == ref]
    if exact:
        return exact

    # 2) title 子串。
    by_title = [n for n in sections if ref in (n.title or "").lower()]
    if by_title:
        return by_title

    # 3) 体裁推断。
    ref_type = infer_section_type(ref, ref)
    if ref_type is not SectionType.UNKNOWN:
        by_type = [
            n
            for n in sections
            if infer_section_type(n.section_id, n.title) is ref_type
        ]
        if by_type:
            return by_type

    return []


def _handle_locate(ctx: ToolContext, reference: str) -> str:
    matches = find_section_matches(ctx.workspace, reference)

    if not matches:
        ctx.session.record("locate_section", reference=reference, result="not_found")
        return f"未找到与「{reference}」匹配的章节。当前章节：" + _list_titles(ctx.workspace)

    if len(matches) == 1:
        node = matches[0]
        ctx.session.record(
            "locate_section", reference=reference, matched=node.section_id
        )
        return f"命中唯一章节：id={node.section_id}，标题《{node.title}》。"

    candidates = "；".join(f"id={n.section_id}《{n.title}》" for n in matches)
    ctx.session.record(
        "locate_section", reference=reference, result="ambiguous", count=len(matches)
    )
    return (
        f"「{reference}」匹配到多个候选，需澄清（请调用 ask_user 让作者确认）："
        f"{candidates}。"
    )


def _list_titles(ws: PaperWorkspace) -> str:
    titles = [f"id={n.section_id}《{n.title}》" for n in ws.ordered_sections()]
    return "、".join(titles) if titles else "（无章节）"


def register_locate_section(registry: ToolRegistry, ctx: ToolContext) -> None:
    """把 locate_section 工具注册进 registry。"""
    registry.register(
        name="locate_section",
        description=_LOCATE_DESCRIPTION,
        handler=lambda reference: _handle_locate(ctx, reference),
        parameters=_LOCATE_SCHEMA,
    )


__all__ = ["find_section_matches", "register_locate_section"]
