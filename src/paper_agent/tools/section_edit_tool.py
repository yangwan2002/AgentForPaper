"""章节级精确编辑工具（升级 Req 6 / Req 9）。

供写作智能体在 ReAct 工具循环中调用：通过锚文本（anchor）定位章节内的具体位置，
按 mode 做"替换片段 / 插入"而非整章重写，从而提升编辑准确性、降低误改风险。

设计契约（与 LiteratureSearchTool 一致的累积器模式）：
- 工具只读工作区投影来校验锚点，**绝不直接写工作区**；
- 校验通过时仅把 `SectionEdit` 意图累积到 `self.edits`，由 WritingAgent
  后续汇聚为 `WorkspaceMutation` 原子落盘（保持「智能体不直接写工作区」契约）；
- 任何错误路径（章节不存在 / 锚点未命中 / 多处命中 / mode 非法）都返回明确
  错误文本且不产生任何累积或工作区变更（Property 9：局部编辑不外溢）。
"""

from __future__ import annotations

from paper_agent.tools.registry import ToolRegistry
from paper_agent.workspace.models import PaperWorkspace, SectionEdit

# 受限的编辑模式集合（Req 6.6）。
_VALID_MODES = ("replace", "insert_after", "insert_before")

_EDIT_SCHEMA = {
    "type": "object",
    "properties": {
        "section_id": {
            "type": "string",
            "description": "目标章节 id（必须已存在于工作区）",
        },
        "anchor": {
            "type": "string",
            "description": "定位锚文本，必须在目标章节内容中唯一出现一次",
        },
        "replacement": {
            "type": "string",
            "description": "替换或插入的文本",
        },
        "mode": {
            "type": "string",
            "enum": list(_VALID_MODES),
            "description": "编辑模式：replace 替换锚点片段；"
            "insert_after 在锚点后插入；insert_before 在锚点前插入。默认 replace。",
        },
    },
    "required": ["section_id", "anchor", "replacement"],
}


class SectionEditTool:
    """章节级精确编辑：校验锚点并累积结构化编辑意图（不直接写工作区）。"""

    def __init__(self, workspace: PaperWorkspace) -> None:
        # 只读取工作区做锚点校验，工具不持有写入权限。
        self._ws = workspace
        self.edits: list[SectionEdit] = []

    def edit_section(
        self,
        section_id: str,
        anchor: str,
        replacement: str,
        mode: str = "replace",
    ) -> str:
        # 1) mode 合法性（Req 1.3 / Req 6.6）——防御式显式校验：mode 必须严格
        #    属于 {replace, insert_after, insert_before}。非法 mode 在此处提前
        #    返回，函数直接结束，因此：(a) 不会走到步骤 4 的 self.edits.append，
        #    即不累积任何编辑意图；(b) 全程只读工作区、从不写入，目标章节内容
        #    保持字节级不变；(c) 返回明确指示 mode 非法的错误文本（含非法取值与
        #    允许取值集合）。此顺序刻意置于所有校验之首，保证非法 mode 永不触及
        #    章节内容或锚点逻辑。
        if mode not in _VALID_MODES:
            return (
                f"编辑失败：mode 非法（{mode!r}），未做任何变更。"
                f"允许的取值为 {', '.join(_VALID_MODES)}。"
            )

        # 2) section_id 存在性（Req 9.4）——不存在则拒绝，不产生 mutation。
        draft = self._ws.section_drafts.get(section_id)
        if draft is None:
            return f"编辑失败：章节 {section_id!r} 不存在，未做任何变更。"

        # 3) 锚点命中次数校验（Req 6.4 / 6.5 / 6.12）。
        content = draft.content
        hits = content.count(anchor) if anchor else 0
        if hits == 0:
            return (
                f"编辑失败：锚文本在章节 {section_id!r} 中未命中，"
                f"未做任何变更。请提供章节内确实存在的锚文本。"
            )
        if hits > 1:
            return (
                f"编辑失败：锚文本在章节 {section_id!r} 中命中 {hits} 处（不唯一），"
                f"未做任何变更。请提供更长、能唯一定位的锚文本。"
            )

        # 4) 唯一命中（== 1）——产出并累积 SectionEdit 意图（不直接写工作区）。
        edit = SectionEdit(
            section_id=section_id,
            anchor=anchor,
            replacement=replacement,
            mode=mode,
        )
        self.edits.append(edit)
        return (
            f"已记录对章节 {section_id!r} 的编辑意图（mode={mode}），"
            f"将由写作智能体汇聚后原子落盘。"
        )


def build_section_edit_tool(
    workspace: PaperWorkspace,
) -> tuple[ToolRegistry, SectionEditTool]:
    """构造章节编辑工具集与编辑意图累积器。"""
    tool = SectionEditTool(workspace)
    registry = ToolRegistry()
    registry.register(
        name="edit_section",
        description="对章节做锚点定位的精确编辑（替换片段或插入，而非整章重写）。"
        "anchor 必须在目标章节内唯一出现一次，否则返回错误且不产生变更。",
        handler=tool.edit_section,
        parameters=_EDIT_SCHEMA,
    )
    return registry, tool
