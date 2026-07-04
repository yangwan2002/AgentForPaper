"""grounding 文本组装（引用忠实性审计 · 纯函数）。

本模块把某被引文献的**可用来源文本**组装成供 ``Faithfulness_Judge`` 阅读的
``Grounding_Text``。设计契约（对齐 design.md 的 GroundingAssembler 与
Requirements 2.1 / 2.2 / 2.3 / 2.4 / 2.6 / 7.4 / 9.3）：

- **仅取材于被引文献自身**：``ref.title`` + ``ref.abstract`` +
  ``ref.abstract_sections``，绝不引入该文献之外的任何文本（Req 2.1 / 2.4）。
- **复用而非重造**：段落抽取复用 ``paper_section_tool.extract_section``，不新增
  第二套段落抽取实现（Req 2.2 / 9.3）。
- **确定性拼接顺序**：title → 命中的段落（按 ``section_hints`` 顺序、去重）→
  abstract 整段兜底；去重保证同一段文本不被重复拼接。
- **防御式截断**：``strip`` 后按 ``token_budget`` 上限字符数截断（Req 2.6 / 7.4）。
- **绝不调用 LLM**：本函数是纯函数，无任何 I/O，不使用模型参数化记忆（Req 2.4）。
"""

from __future__ import annotations

from paper_agent.tools.paper_section_tool import extract_section, slice_section
from paper_agent.workspace.models import ReferenceEntry

# 默认段落提示：覆盖方法 / 结果 / 动机 / 结论四类常见段落。
_DEFAULT_SECTION_HINTS: tuple[str, ...] = (
    "method",
    "results",
    "motivation",
    "conclusion",
)


def assemble_grounding(
    ref: ReferenceEntry,
    *,
    token_budget: int,
    section_hints: tuple[str, ...] = _DEFAULT_SECTION_HINTS,
) -> str:
    """从 ``ref`` 组装 grounding 文本（纯函数）。

    取材范围（均属被引文献自身）：``ref.title`` + ``ref.abstract`` +
    ``ref.abstract_sections`` +（Round 9）``ref.full_text``（正文全文，若已富化）。
    复用 ``extract_section`` / ``slice_section`` 抽取命中段落，确定性去重拼接后
    ``strip`` 并按 ``token_budget`` 上限字符数截断。``full_text`` 为空时行为与旧版
    逐字节一致（仍只到 abstract 层）。

    Args:
        ref: 被引文献记录；只读，不改动。
        token_budget: 组装结果的字符数上限（防御式截断，见 Req 2.6 / 7.4）。
        section_hints: 依序尝试抽取的段落名；命中者按此顺序纳入 grounding。

    Returns:
        组装并截断后的 grounding 字符串；无任何可用来源时返回空字符串。
    """
    parts: list[str] = []
    # 已纳入片段的规范化集合，用于去重（避免 title/段落/abstract 内容重复拼接）。
    seen: set[str] = set()

    def _add(text: str | None) -> None:
        if not text:
            return
        stripped = text.strip()
        if not stripped:
            return
        key = stripped.casefold()
        if key in seen:
            return
        seen.add(key)
        parts.append(stripped)

    # 1) 标题。
    _add(ref.title)

    # 2) 命中的段落，按 section_hints 顺序、去重（复用 extract_section）。
    for name in section_hints:
        _add(extract_section(ref, name))

    # 3) 正文全文（Round 9）：若已富化，按同样的段落提示从正文切片取材——
    #    这是消解「细节声明在正文而非 abstract」假阴的关键来源。为空时本步无产出，
    #    grounding 逐字节回落到 abstract 层（行为不变）。
    full_text = getattr(ref, "full_text", "") or ""
    if full_text.strip():
        for name in section_hints:
            _add(slice_section(full_text, name))

    # 4) abstract 整段兜底（若尚未通过段落抽取纳入相同文本）。
    _add(ref.abstract)

    grounding = "\n\n".join(parts).strip()

    # 4) 防御式截断至 token_budget 上限字符数。
    if token_budget >= 0 and len(grounding) > token_budget:
        grounding = grounding[:token_budget]

    return grounding


__all__ = ["assemble_grounding"]
