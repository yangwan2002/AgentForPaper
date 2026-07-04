"""只读工作区访问工具（升级 Req 6.2 / 6.3 / 6.10 / 6.11）。

为模型提供「按需取材」的只读工具，避免预先把章节全文与文献元数据
全部塞进上下文。设计要点：

- `WorkspaceView` 是 `PaperWorkspace` 的只读投影：只暴露读取接口，
  且读取返回的是数据副本，调用方无法借此改动底层工作区
  （保持「智能体不直接写工作区」的契约）。
- `WorkspaceReadTools` 封装 `WorkspaceView`，把读取结果格式化为字符串，
  直接适配 LLM 工具循环（工具结果会被字符串化回填）。
- 命中：返回章节全文（标题 + 正文）/ 文献完整元数据，且不变更工作区。
- 未命中：返回明确的错误字符串（不抛入工作区、不做任何变更）。
"""

from __future__ import annotations

import copy

from paper_agent.workspace.models import (
    PaperWorkspace,
    ReferenceEntry,
    SectionDraft,
)


class WorkspaceView:
    """工作区只读投影。

    仅暴露读取接口；不提供任何写入/变更方法。读取通过返回深拷贝的方式，
    确保调用方对返回对象的任何修改都不会影响底层 `PaperWorkspace`。
    """

    def __init__(self, workspace: PaperWorkspace) -> None:
        # 持有引用以反映工作区的最新状态；读取时再做副本隔离。
        self._ws = workspace

    def get_section(self, section_id: str) -> SectionDraft | None:
        """按 id 返回章节草稿副本；不存在返回 None。不变更工作区。"""
        draft = self._ws.section_drafts.get(section_id)
        if draft is None:
            return None
        return copy.deepcopy(draft)

    def get_reference(self, reference_id: str) -> ReferenceEntry | None:
        """按 id 返回文献条目副本；不存在返回 None。不变更工作区。"""
        for ref in self._ws.verified_references:
            if ref.id == reference_id:
                return copy.deepcopy(ref)
        return None

    def section_ids(self) -> list[str]:
        """返回当前所有章节 id（便于错误信息中提示可选项）。"""
        return list(self._ws.section_drafts.keys())

    def reference_ids(self) -> list[str]:
        """返回当前所有文献 id。"""
        return [r.id for r in self._ws.verified_references]


class WorkspaceReadTools:
    """只读工作区访问工具，供模型按需取材。

    返回字符串结果以适配 function calling 工具循环。所有方法均为只读，
    不会变更工作区。
    """

    def __init__(self, ws_view: WorkspaceView) -> None:
        self._view = ws_view

    def read_section(self, section_id: str) -> str:
        """读取指定章节全文（标题 + 正文）。

        命中：返回章节标题与完整正文，且不变更工作区（Req 6.2）。
        未命中：返回明确错误字符串，且不变更工作区（Req 6.10）。
        """
        sid = (section_id or "").strip()
        if not sid:
            return "错误：read_section 需要非空的 section_id。"

        draft = self._view.get_section(sid)
        if draft is None:
            available = self._view.section_ids()
            hint = "（当前无任何章节）" if not available else f"（可选 section_id：{', '.join(available)}）"
            return f"错误：未找到 section_id 为 '{sid}' 的章节。{hint}"

        cited = ", ".join(draft.cited_reference_ids) if draft.cited_reference_ids else "（无）"
        content = draft.content if draft.content.strip() else "（正文为空）"
        return (
            f"section_id: {draft.section_id}\n"
            f"标题: {draft.title}\n"
            f"引用文献 id: {cited}\n"
            f"正文:\n{content}"
        )

    def read_reference(self, reference_id: str) -> str:
        """读取某条参考文献的完整元数据。

        命中：返回完整元数据，且不变更工作区（Req 6.3）。
        未命中：返回明确错误字符串，且不变更工作区（Req 6.11）。
        """
        rid = (reference_id or "").strip()
        if not rid:
            return "错误：read_reference 需要非空的 reference_id。"

        ref = self._view.get_reference(rid)
        if ref is None:
            available = self._view.reference_ids()
            hint = "（当前无任何文献）" if not available else f"（可选 reference_id：{', '.join(available)}）"
            return f"错误：未找到 reference_id 为 '{rid}' 的文献。{hint}"

        authors = ", ".join(ref.authors) if ref.authors else "（未知作者）"
        year = str(ref.year) if ref.year is not None else "（未知年份）"
        abstract = ref.abstract if ref.abstract.strip() else "（无摘要）"
        return (
            f"id: {ref.id}\n"
            f"标题: {ref.title}\n"
            f"作者: {authors}\n"
            f"年份: {year}\n"
            f"来源: {ref.source or '（未知）'}\n"
            f"source_id: {ref.source_id}\n"
            f"已核验: {'是' if ref.verified else '否'}\n"
            f"摘要:\n{abstract}"
        )
