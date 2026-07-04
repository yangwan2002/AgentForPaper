"""论文文本视图的统一组装（#16）。

此前 `ReviewAgent._assemble_paper` 与写作智能体各自拼接论文文本，口径不一、
改一处漏一处。此处收敛为单一 ``assemble_paper_text``，供评审等需要"整篇文本"
的调用方复用。写作智能体的运行上下文是「按章节摘要 + 大纲」的裁剪视图，与本
全量视图职责不同，不复用此处实现。
"""

from __future__ import annotations

from paper_agent.workspace.models import PaperWorkspace


def assemble_paper_text(ws: PaperWorkspace) -> str:
    """把工作区各章节草稿按大纲顺序拼成整篇文本（含章节标题与 section_id）。"""
    parts: list[str] = []
    for node in ws.ordered_sections():
        draft = ws.section_drafts.get(node.section_id)
        if draft:
            parts.append(f"## [{node.section_id}] {node.title}\n{draft.content}")
    return "\n\n".join(parts)


__all__ = ["assemble_paper_text"]
