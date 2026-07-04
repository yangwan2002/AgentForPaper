"""add_references 工具：按主题检索并向论文增补可核验的参考文献（写工具）。

流程：调用既有 ``LiteratureSearchTool`` 检索+核验 → 取本次新获得的候选 → 构造
``CHANGE_CITATION`` 的 ``ProposedChange`` → 经 ``commit`` 落盘。护栏闸门在落盘前
**逐条再核验**（防御纵深），只接受可核验者；数量不足时产差额说明而非虚构填充
（Req 4.1/4.2/4.3）。

依赖注入：检索能力（``LiteratureSearchTool``）在注册时注入，保持 ``ToolContext`` 精简。
"""

from __future__ import annotations

from paper_agent.agent_platform.apply import commit
from paper_agent.agent_platform.models import CHANGE_CITATION, ProposedChange
from paper_agent.agent_platform.tools.context import ToolContext
from paper_agent.tools.citation import CitationVerifier
from paper_agent.tools.citation_parser import CitationParser
from paper_agent.tools.literature_tool import LiteratureSearchTool
from paper_agent.tools.registry import ToolRegistry
from paper_agent.workspace.models import PaperWorkspace, ReferenceEntry

_ADD_REFS_SCHEMA = {
    "type": "object",
    "properties": {
        "query": {
            "type": "string",
            "description": "检索主题关键词（建议英文、聚焦具体领域）。",
        },
        "limit": {
            "type": "integer",
            "description": "希望增补的文献条数上限，默认 5。",
        },
    },
    "required": ["query"],
}

_ADD_REFS_DESCRIPTION = (
    "按主题检索真实学术文献并增补到论文的已验证文献库，供后续引用。系统会逐条核验"
    "真实性，只增补可核验的文献；若可核验数量不足你的请求，会如实告知差额，绝不"
    "以虚构文献填充。"
)


def _next_citation_number(ws: PaperWorkspace) -> int:
    """已验证文献中数字编号的最大值 + 1（供新增文献续编，保证正文 [n] 可对上）。"""
    nums = [int(r.id) for r in ws.verified_references if str(r.id).isdigit()]
    return (max(nums) + 1) if nums else 1


def _handle_add_references(
    ctx: ToolContext, search_tool: LiteratureSearchTool, query: str, limit: int = 5
) -> str:
    before = set(search_tool.found.keys())
    search_msg = search_tool.search(query, limit=int(limit))

    # 取本次调用新获得的已验证候选（search_tool.found 跨调用累积）。
    new_refs = [
        ref for rid, ref in search_tool.found.items() if rid not in before
    ]
    if not new_refs:
        ctx.session.record("add_references", query=query, added=0)
        return f"未检索到可增补的新文献。检索反馈：{search_msg}"

    # 关键：把新文献重新编号为连续数字 id（续接现有最大编号），使 agent 在正文用
    # [n] 引用时能对上已验证库——这是之前"引言引用符号被判非法而删掉"的根因。
    start = _next_citation_number(ctx.workspace)
    renumbered = [
        ReferenceEntry(**{**vars(ref), "id": str(start + i)})
        for i, ref in enumerate(new_refs)
    ]

    change = ProposedChange(
        mutation=lambda ws: None,  # 引用通道由护栏闸门自行合成落盘意图。
        kind=CHANGE_CITATION,
        references=renumbered,
        describe=f"增补文献（query={query}）",
    )
    before_ids = {r.id for r in ctx.workspace.verified_references}
    outcome = commit(ctx.repo, ctx.workspace, ctx.gate, [change])
    added = [r for r in ctx.workspace.verified_references if r.id not in before_ids]

    ctx.session.record(
        "add_references", query=query, requested=len(new_refs), added=len(added)
    )
    if not added:
        return f"本次未增补到可核验文献。检索反馈：{search_msg}"

    lines = [_citable_line(r) for r in added]
    msg = (
        f"已增补 {len(added)} 篇可核验文献，正文可用以下编号引用（务必用对应编号）：\n"
        + "\n".join(lines)
    )
    if outcome.notes:
        msg += "\n" + " ".join(outcome.notes)
    return msg


def _citable_line(ref: ReferenceEntry) -> str:
    authors = ", ".join((ref.authors or [])[:2]) or "佚名"
    year = ref.year if ref.year is not None else "n.d."
    return f"[{ref.id}] {authors}. {ref.title} ({year})"


# --------------------------------------------------------------------------- #
# verify_existing_references：把原文已有参考文献核验后纳入已验证库
# --------------------------------------------------------------------------- #

_VERIFY_REFS_SCHEMA = {"type": "object", "properties": {}, "required": []}

_VERIFY_REFS_DESCRIPTION = (
    "核验论文原文自带的参考文献列表（按标题/DOI 回查真实性），把真实存在的文献纳入"
    "已验证文献库，并**保留原文编号**（原文 [1] 即以编号 1 入库）。当你要在正文"
    "（如引言、相关工作）引用论文里已列出的文献时，先调用此工具，之后即可用 [编号] 引用。"
    "无法核验的条目不会入库（可能不存在或信息不足）。"
)


def _build_verified_entry(index: int, ref, matched) -> ReferenceEntry:
    """据核验结果构造入库条目：保留原文编号作 id，元数据优先取真实记录。"""
    meta = matched if matched is not None else ref
    return ReferenceEntry(
        id=str(index),                       # 保留原文编号，正文 [index] 才能对上
        title=meta.title,
        authors=list(getattr(meta, "authors", []) or []),
        year=getattr(meta, "year", None),
        source_id=getattr(meta, "source_id", "") or getattr(ref, "source_id", ""),
        source=getattr(meta, "source", "") or "draft",
        verified=True,
    )


def _handle_verify_existing_references(
    ctx: ToolContext, verifier: CitationVerifier
) -> str:
    draft = ctx.workspace.original_draft or ""
    if not draft.strip():
        return "工作区没有原文内容，无法核验参考文献。请先用 import_draft 导入论文。"

    parsed = CitationParser().parse(draft)  # 正则解析（无 LLM 也可用）
    if not parsed.references:
        return "未在原文中解析到参考文献列表（可能无「参考文献」小节或格式特殊）。"

    verified: list[ReferenceEntry] = []
    unverifiable = 0
    for i, ref in enumerate(parsed.references, start=1):
        try:
            result = verifier.verify_by_metadata(ref)
        except Exception:  # noqa: BLE001 - 核验失败按不可核验处理，不中断
            result = None
        if result is not None and result.exists:
            verified.append(_build_verified_entry(i, ref, result.matched))
        else:
            unverifiable += 1

    if verified:
        ctx.repo.update(ctx.workspace, _append_verified_mutation(verified))

    ctx.session.record(
        "verify_existing_references",
        total=len(parsed.references),
        verified=len(verified),
        unverifiable=unverifiable,
    )
    ids = "、".join(r.id for r in verified)
    return (
        f"共解析到 {len(parsed.references)} 条原文参考文献，核验入库 {len(verified)} 条"
        f"（编号 {ids or '无'}），{unverifiable} 条无法核验（未入库，切勿引用）。"
        f"现在可在正文用 [编号] 引用已入库的文献。"
    )


def _append_verified_mutation(entries: list[ReferenceEntry]):
    """把已核验文献并入 verified_references（按 id 去重）。"""

    def _mutate(ws: PaperWorkspace) -> None:
        existing = {r.id for r in ws.verified_references}
        for entry in entries:
            if entry.id not in existing:
                ws.verified_references.append(entry)
                existing.add(entry.id)

    return _mutate


def register_verify_existing_references(
    registry: ToolRegistry, ctx: ToolContext, verifier: CitationVerifier
) -> None:
    """注册 verify_existing_references 工具（核验器经注入）。"""
    registry.register(
        name="verify_existing_references",
        description=_VERIFY_REFS_DESCRIPTION,
        handler=lambda: _handle_verify_existing_references(ctx, verifier),
        parameters=_VERIFY_REFS_SCHEMA,
    )


def register_add_references(
    registry: ToolRegistry, ctx: ToolContext, search_tool: LiteratureSearchTool
) -> None:
    """注册 add_references 工具（检索能力经 ``search_tool`` 注入）。"""
    registry.register(
        name="add_references",
        description=_ADD_REFS_DESCRIPTION,
        handler=lambda query, limit=5: _handle_add_references(
            ctx, search_tool, query, limit
        ),
        parameters=_ADD_REFS_SCHEMA,
    )


__all__ = ["register_add_references", "register_verify_existing_references"]
