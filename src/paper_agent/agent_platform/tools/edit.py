"""章节内容改写类写工具：``rewrite_section`` / ``polish_section`` / ``edit_section_anchor``。

设计取舍（thin-tool，贴近 Claude/Cursor 的智能体范式）：认知工作（写什么、怎么润色）
由顶层 LLM 完成，工具只负责**把改动施加到目标章节并经护栏落盘**。因此改写与润色
共用同一效应器（替换章节正文），仅描述与 transcript 标签不同，指导 LLM 表达意图。

统一写路径：所有工具只构造 ``ProposedChange`` 并经 ``apply.commit`` 落盘，绝不直接
写工作区（Req 6.1）。``Section_Scope_Task`` 的改动严格限定在目标章节，范围外不产生
任何意图（Req 3.1/3.2）。护栏未通过时返回被拒原因，供 LLM 修正（Req 5.3）。
"""

from __future__ import annotations

from paper_agent.agent_platform.apply import commit
from paper_agent.agent_platform.models import (
    CHANGE_CONTENT,
    GateOutcome,
    ProposedChange,
)
from paper_agent.agent_platform.tools.context import ToolContext
from paper_agent.prompts.section_types import SectionType, infer_section_type
from paper_agent.tools.registry import ToolRegistry
from paper_agent.tools.section_edit_tool import SectionEditTool
from paper_agent.workspace.models import (
    OutlineNode,
    PaperWorkspace,
    SectionDraft,
    SectionEdit,
)

_VALID_MODES = ("replace", "insert_after", "insert_before")


# --------------------------------------------------------------------------- #
# 共享：提交并汇报
# --------------------------------------------------------------------------- #

def _commit_and_report(ctx: ToolContext, change: ProposedChange, label: str) -> str:
    """经单一写路径提交一个改动，返回面向 LLM 的结果说明。"""
    outcome = commit(ctx.repo, ctx.workspace, ctx.gate, [change])
    ctx.session.record(
        label,
        section_id=change.section_id,
        passed=outcome.passed,
        rejected=len(outcome.rejected),
    )
    return _format_outcome(outcome, change.section_id, label)


def _format_outcome(outcome: GateOutcome, section_id: str, label: str) -> str:
    if outcome.passed and outcome.accepted_mutations:
        msg = f"已完成「{label}」并落盘（章节 {section_id}）。"
    elif outcome.rejected:
        reasons = "；".join(r.reason for r in outcome.rejected)
        msg = (
            f"「{label}」未通过护栏，未落盘（章节 {section_id}）。原因：{reasons}。"
            f"请据原因修正后重试。"
        )
    else:
        msg = f"「{label}」未产生可落盘的改动（章节 {section_id}）。"
    if outcome.notes:
        msg += " " + " ".join(outcome.notes)
    return msg


# --------------------------------------------------------------------------- #
# rewrite_section / polish_section（整章正文替换效应器）
# --------------------------------------------------------------------------- #

def _replace_content_mutation(section_id: str, new_content: str):
    """构造「替换目标章节正文」的更新意图（仅动目标章节，Req 3.2）。"""

    def _mutate(ws: PaperWorkspace) -> None:
        draft = ws.section_drafts.get(section_id)
        if draft is not None:
            draft.content = new_content

    return _mutate


# 参考文献类章节的标题/ id 关键词（命中即禁止整段改写，防止编造文献）。
_REFERENCE_KEYWORDS = ("参考文献", "references", "reference", "bibliography", "文献")


def _is_reference_section(ws: PaperWorkspace, section_id: str) -> bool:
    """判断某章节是否为参考文献章节（按 id/标题关键词）。"""
    draft = ws.section_drafts.get(section_id)
    title = (draft.title if draft else "") or ""
    hay = f"{section_id} {title}".lower()
    return any(kw in hay for kw in _REFERENCE_KEYWORDS)


def _handle_set_content(
    ctx: ToolContext, section_id: str, new_content: str, label: str
) -> str:
    if section_id not in ctx.workspace.section_drafts:
        return f"操作失败：章节 {section_id!r} 不存在，未做任何变更。"
    if not (new_content or "").strip():
        return "操作失败：新正文为空，未做任何变更。"
    # 学术诚信红线：禁止整段改写参考文献（极易凭记忆编造/篡改作者、标题）。
    # 局部修正请用 edit_section_anchor（锚定已有文本，无法凭空捏造）。
    if _is_reference_section(ctx.workspace, section_id):
        return (
            f"已拒绝：不允许整段改写参考文献章节（{section_id}），以防编造或篡改文献信息。"
            f"如需修正个别条目（如特殊字符），请用 edit_section_anchor 做锚点级局部编辑；"
            f"如原文某条显示不全，请保持原样或询问作者，切勿臆造。"
        )
    change = ProposedChange(
        mutation=_replace_content_mutation(section_id, new_content),
        kind=CHANGE_CONTENT,
        section_id=section_id,
        describe=label,
    )
    return _commit_and_report(ctx, change, label)


_REWRITE_SCHEMA = {
    "type": "object",
    "properties": {
        "section_id": {"type": "string", "description": "目标章节 id"},
        "new_content": {
            "type": "string",
            "description": "改写后的完整章节正文（由你撰写）。仅可引用已核验文献的 [id]。",
        },
    },
    "required": ["section_id", "new_content"],
}

_POLISH_SCHEMA = {
    "type": "object",
    "properties": {
        "section_id": {"type": "string", "description": "目标章节 id"},
        "new_content": {
            "type": "string",
            "description": "润色后的完整章节正文；只改语言表达，不改变原意与事实。",
        },
    },
    "required": ["section_id", "new_content"],
}


def register_rewrite_section(registry: ToolRegistry, ctx: ToolContext) -> None:
    """整章改写：用你撰写的新正文替换目标章节（如改变叙述方式、补充论证）。"""
    registry.register(
        name="rewrite_section",
        description=(
            "用你撰写的新正文替换某章节的全部内容，用于改变叙述方式、结构或补充"
            "论证。改动仅作用于目标章节，且会经学术正确性护栏校验后才落盘。"
        ),
        handler=lambda section_id, new_content: _handle_set_content(
            ctx, section_id, new_content, "改写章节"
        ),
        parameters=_REWRITE_SCHEMA,
    )


def register_polish_section(registry: ToolRegistry, ctx: ToolContext) -> None:
    """章节润色：用你润色后的正文替换目标章节（只改语言、不改原意）。"""
    registry.register(
        name="polish_section",
        description=(
            "用你润色后的正文替换某章节内容，仅改进语言表达而不改变原意与事实。"
            "改动仅作用于目标章节，且会经护栏校验后才落盘。"
        ),
        handler=lambda section_id, new_content: _handle_set_content(
            ctx, section_id, new_content, "润色章节"
        ),
        parameters=_POLISH_SCHEMA,
    )


# --------------------------------------------------------------------------- #
# edit_section_anchor（锚点精确编辑）
# --------------------------------------------------------------------------- #

def apply_section_edit(content: str, edit: SectionEdit) -> str:
    """把一个已校验的 ``SectionEdit`` 施加到章节正文，返回新正文（只替换首处）。"""
    if edit.mode == "replace":
        return content.replace(edit.anchor, edit.replacement, 1)
    if edit.mode == "insert_after":
        return content.replace(edit.anchor, edit.anchor + edit.replacement, 1)
    if edit.mode == "insert_before":
        return content.replace(edit.anchor, edit.replacement + edit.anchor, 1)
    return content  # 防御式：非法 mode 已在校验阶段拦截，此处不改动。


def _anchor_edit_mutation(section_id: str, edit: SectionEdit):
    def _mutate(ws: PaperWorkspace) -> None:
        draft = ws.section_drafts.get(section_id)
        if draft is not None:
            draft.content = apply_section_edit(draft.content, edit)

    return _mutate


def _handle_anchor_edit(
    ctx: ToolContext,
    section_id: str,
    anchor: str,
    replacement: str,
    mode: str = "replace",
) -> str:
    # 复用既有 SectionEditTool 做锚点唯一性/存在性/mode 校验（只读工作区）。
    validator = SectionEditTool(ctx.workspace)
    message = validator.edit_section(section_id, anchor, replacement, mode)
    if not validator.edits:
        # 校验未通过：SectionEditTool 已返回明确错误文本，原样透传。
        return message

    edit = validator.edits[-1]
    change = ProposedChange(
        mutation=_anchor_edit_mutation(section_id, edit),
        kind=CHANGE_CONTENT,
        section_id=section_id,
        describe="锚点编辑",
    )
    return _commit_and_report(ctx, change, "锚点编辑")


_ANCHOR_SCHEMA = {
    "type": "object",
    "properties": {
        "section_id": {"type": "string", "description": "目标章节 id（须已存在）"},
        "anchor": {
            "type": "string",
            "description": "定位锚文本，须在目标章节内唯一出现一次",
        },
        "replacement": {"type": "string", "description": "替换或插入的文本"},
        "mode": {
            "type": "string",
            "enum": list(_VALID_MODES),
            "description": "replace 替换锚点片段；insert_after/insert_before 前后插入。默认 replace。",
        },
    },
    "required": ["section_id", "anchor", "replacement"],
}


def register_edit_section_anchor(registry: ToolRegistry, ctx: ToolContext) -> None:
    """锚点精确编辑：定位章节内唯一锚文本做替换/插入（局部改动，非整章重写）。"""
    registry.register(
        name="edit_section_anchor",
        description=(
            "对章节做锚点定位的精确编辑（替换片段或前后插入），适合小范围修改。"
            "anchor 须在目标章节内唯一出现，否则返回错误且不产生变更。改动经护栏校验后落盘。"
        ),
        handler=lambda section_id, anchor, replacement, mode="replace": _handle_anchor_edit(
            ctx, section_id, anchor, replacement, mode
        ),
        parameters=_ANCHOR_SCHEMA,
    )


# --------------------------------------------------------------------------- #
# add_section（新增章节，如补写缺失的引言）
# --------------------------------------------------------------------------- #

def _new_section_id(ws: PaperWorkspace, title: str) -> str:
    """为新章节生成稳定且不冲突的 id：优先用体裁名（引言→introduction），否则序号。"""
    stype = infer_section_type(title, title)
    base = stype.value if stype is not SectionType.UNKNOWN else "section"
    if base not in ws.section_drafts:
        return base
    i = 2
    while f"{base}_{i}" in ws.section_drafts:
        i += 1
    return f"{base}_{i}"


def _add_section_mutation(section_id: str, title: str, content: str, position: str):
    """构造新增章节的更新意图（大纲节点 + 章节草稿；position 决定排在首/尾）。"""

    def _mutate(ws: PaperWorkspace) -> None:
        orders = [n.order for n in ws.outline] or [0]
        order = (min(orders) - 1) if position == "start" else (max(orders) + 1)
        ws.outline.append(OutlineNode(section_id=section_id, title=title, order=order))
        ws.section_drafts[section_id] = SectionDraft(
            section_id=section_id, title=title, content=content
        )

    return _mutate


def _handle_add_section(
    ctx: ToolContext, title: str, content: str, position: str = "end"
) -> str:
    if not (title or "").strip():
        return "操作失败：章节标题为空。"
    if not (content or "").strip():
        return "操作失败：章节内容为空。"
    section_id = _new_section_id(ctx.workspace, title.strip())
    change = ProposedChange(
        mutation=_add_section_mutation(section_id, title.strip(), content, position),
        kind=CHANGE_CONTENT,
        section_id=section_id,
        describe=f"新增章节《{title}》",
    )
    return _commit_and_report(ctx, change, f"新增章节《{title.strip()}》")


_ADD_SECTION_SCHEMA = {
    "type": "object",
    "properties": {
        "title": {"type": "string", "description": "新章节标题，如「引言」。"},
        "content": {
            "type": "string",
            "description": "新章节的完整正文（由你撰写）。仅可引用已核验文献的 [id]。",
        },
        "position": {
            "type": "string",
            "enum": ["start", "end"],
            "description": "排在全文开头（如引言）还是末尾。默认 end。",
        },
    },
    "required": ["title", "content"],
}


def register_add_section(registry: ToolRegistry, ctx: ToolContext) -> None:
    """新增章节：为论文补写一个不存在的章节（如缺失的引言/结论）。"""
    registry.register(
        name="add_section",
        description=(
            "为论文新增一个当前不存在的章节（如补写缺失的引言、结论）。你需要撰写该"
            "章节的完整正文，改动会经学术正确性护栏校验后落盘。修改已有章节请用"
            "rewrite_section/polish_section。"
        ),
        handler=lambda title, content, position="end": _handle_add_section(
            ctx, title, content, position
        ),
        parameters=_ADD_SECTION_SCHEMA,
    )


__all__ = [
    "apply_section_edit",
    "register_rewrite_section",
    "register_polish_section",
    "register_edit_section_anchor",
    "register_add_section",
]
