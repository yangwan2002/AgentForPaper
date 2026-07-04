"""导出期引用闭合：参考文献表只列被正文实际引用的文献。

问题背景：文献库可能积累远多于正文实际引用的候选文献（如检索/审计阶段入库
80 篇，但正文只引用了 20 篇）。若导出时把整库都列进参考文献表，成稿会出现
"参考文献远多于正文引用"的失真。

本模块提供一个**纯函数** :func:`cited_references`，供 docx/latex/markdown 三个
导出器在构建参考文献表前统一调用：扫描各章节正文的 ``[id]`` 标注与章节记录的
``cited_reference_ids``，只保留被引用的已验证文献，并**保持 ``verified_references``
的既定顺序**（编号稳定、可复现）。

设计取舍：闭合只作用于「参考文献表」这一段的构建，不改变正文字节——故 Markdown
逐字节契约（Req 7.4/7.6）不被违反；docx/latex 的 ``[id]→[n]``/``\\cite`` 行内渲染
仍各自按其既有格式约定处理，只是编号映射改为基于「被引用子集」。
"""

from __future__ import annotations

from paper_agent.tools.quality_gate import extract_text_citations
from paper_agent.workspace.models import PaperWorkspace, ReferenceEntry


def cited_reference_ids(ws: PaperWorkspace) -> set[str]:
    """扫描各章节正文 ``[id]`` 标注与记录的 ``cited_reference_ids``，返回被引用 id 集合。

    合并两条互补路径，避免「记录未同步正文」或「正文标注未记录」任一遗漏。
    """
    cited: set[str] = set()
    for draft in ws.section_drafts.values():
        cited.update(extract_text_citations(draft.content or ""))
        cited.update(draft.cited_reference_ids)
    return cited


def cited_references(ws: PaperWorkspace) -> list[ReferenceEntry]:
    """返回被正文实际引用的已验证文献，保持 ``verified_references`` 的既定顺序。

    仅包含 ``verified=True`` 且其 id 出现在正文引用集合中的文献；未被引用的
    已验证文献不进入结果（导出参考文献表只列被引用者）。
    """
    cited = cited_reference_ids(ws)
    return [r for r in ws.verified_references if r.verified and r.id in cited]
