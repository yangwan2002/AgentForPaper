"""确定性版面触发判定（visual-layout-acceptance · Task 6）。

判断「本轮任务是否包含版面相关操作」——据此**确定性**决定是否自动触发视觉验收闸，
不调 LLM。目的：绝大多数任务（纯语言润色 / 加引用 / 问答）无版面后果、不该盲跑视觉
校验；只有真的动了版面（转 docx / 分栏 / 宽表跨栏 / 图跨栏 / 字体字号 / 排版）才触发。

关键：该判定**独立于主智能体的自我判断**——主智能体不能靠「不声称做了版面改动」来
跳过对其自身版面产物的校验（Req 11.1/11.4/11.5）。
"""

from __future__ import annotations

# 会产生版面后果的工具名（本轮 transcript 里出现即视为动了版面）。
_LAYOUT_TOOLS = {
    "convert_document",
    "set_typesetting",
    "augment_document",
    "run_python",   # docx 微操（图跨栏 / 字号 / 边距等）多经此
}

# 产物说明（notes）里出现即视为动了版面的关键词（覆盖工具名之外的确定性信号）。
_LAYOUT_NOTE_KEYWORDS = (
    "双栏", "分栏", "跨栏", "宽表", "三线表", "紧凑", "排版", "字体", "字号",
    "行距", "缩进", "页边距", "图", "column",
)


def _entry_text(entry: dict) -> str:
    """把一条 transcript 记录里可能承载版面信号的文本拼起来（notes / files 等）。"""
    parts: list[str] = []
    notes = entry.get("notes")
    if isinstance(notes, str):
        parts.append(notes)
    elif isinstance(notes, (list, tuple)):
        parts.extend(str(n) for n in notes)
    files = entry.get("files")
    if isinstance(files, (list, tuple)):
        parts.extend(str(f) for f in files)
    return " ".join(parts)


def touched_layout(transcript_tail: list[dict]) -> bool:
    """本轮 transcript 是否出现版面相关操作 / 产物信号（确定性，不调 LLM）。

    命中任一条件即为真：
    - 调用了 ``_LAYOUT_TOOLS`` 中的工具；
    - 产物 notes / 文件名里出现版面关键词，且对应了 docx 产物。
    纯语言润色 / 加引用等无匹配 → 返回 False（不触发，省成本）。
    """
    for entry in transcript_tail or []:
        name = str(entry.get("name", ""))
        if name in _LAYOUT_TOOLS:
            return True
        text = _entry_text(entry)
        if ".docx" in text and any(k in text for k in _LAYOUT_NOTE_KEYWORDS):
            return True
    return False


__all__ = ["touched_layout"]
