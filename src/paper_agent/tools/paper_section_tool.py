"""按引用 id + 章节名取相关论文段落的只读工具（Round 6）。

设计动机：写作 Related Work / Method 对比 / Discussion 引用相关工作时，模型若只
看 ``title`` 与 ``abstract`` 整段，写出的引用常是同义改写——抓不到方法机制 /
具体实验结果 / 与本文的差异点。本工具让模型按需取**结构化段落**：

- 优先命中 ``ReferenceEntry.abstract_sections``（来源提供结构化分段时使用，如
  IEEE 的 abstract、部分 OA 仓库给出的 motivation / approach / results 段）；
- 命中不到时回退到对 ``abstract`` 整段做启发式切片（按 motivation / method /
  results / conclusion 的关键词找位置；找不到则按位置等分）。

设计契约：
- **只读**：仅读取工作区 ``verified_references``，绝不写工作区或外部状态；
- 错误路径返回字符串（reference_id 不存在、section 名不在可用集合时给出明确
  提示），不抛异常——适配 LLM function-calling 工具循环；
- 截断到 ``max_chars`` 字符（默认 1500），避免一次塞入过长上下文。
"""

from __future__ import annotations

import re

from paper_agent.tools.registry import ToolRegistry
from paper_agent.workspace.models import PaperWorkspace, ReferenceEntry

# 段落级抽取的最大字符数（防止把整篇 abstract 塞 prompt）。
_DEFAULT_MAX_CHARS = 1500

# 启发式切片的关键词（小写匹配，覆盖中英文）。命中后取该词到下一关键词之间。
_SECTION_KEYWORDS: dict[str, list[str]] = {
    "motivation": [
        "motivation", "background", "introduction", "problem",
        "动机", "背景", "问题",
    ],
    "method": [
        "method", "approach", "we propose", "model", "architecture",
        "方法", "我们提出", "模型",
    ],
    "results": [
        "results", "experiments", "evaluation", "show that", "achieve",
        "结果", "实验", "我们的方法实现", "取得",
    ],
    "conclusion": [
        "conclusion", "in summary", "we conclude", "future work",
        "结论", "综上", "未来工作",
    ],
}

# fetch_paper_section 的 function-calling schema。
FETCH_SCHEMA = {
    "type": "object",
    "properties": {
        "reference_id": {
            "type": "string",
            "description": "要读取段落的参考文献 id（必须在已验证库中）",
        },
        "section": {
            "type": "string",
            "description": (
                "段落名。若文献有结构化 abstract_sections，匹配其键；"
                "否则按 motivation / method / results / conclusion 之一"
                "做启发式切片。"
            ),
        },
        "max_chars": {
            "type": "integer",
            "description": f"返回片段的最大字符数（默认 {_DEFAULT_MAX_CHARS}）",
        },
    },
    "required": ["reference_id", "section"],
}


def _split_by_keywords(text: str, target_keywords: list[str]) -> str | None:
    """在 ``text``（小写化匹配）里找最早命中的关键词，返回从该位置开始的片段。

    片段终点为「下一个段落关键词」的位置（任意类别）或文末，以先到者为准。
    没有任何 target_keywords 命中时返回 None。
    """
    if not text:
        return None
    lower = text.lower()
    # 找 target 命中位置（取最早出现的）。
    start = -1
    for kw in target_keywords:
        idx = lower.find(kw.lower())
        if idx >= 0 and (start == -1 or idx < start):
            start = idx
    if start < 0:
        return None
    # 找下一段落分界点（任意其他类别的关键词，位置 > start）。
    end = len(text)
    for kws in _SECTION_KEYWORDS.values():
        for kw in kws:
            if kw in target_keywords:
                continue
            idx = lower.find(kw.lower(), start + 1)
            if idx > start and idx < end:
                end = idx
    return text[start:end].strip()


def slice_section(text: str, section: str) -> str | None:
    """在任意文本上按 ``section`` 做启发式段落切片；无命中返回 None。

    复用 ``_SECTION_KEYWORDS`` 与 ``_split_by_keywords``，供 abstract 与**正文全文**
    共用同一套切片逻辑（不新增第二套实现）。``section`` 不在已知类别时返回 None。
    """
    section_lower = (section or "").lower().strip()
    if section_lower not in _SECTION_KEYWORDS:
        return None
    return _split_by_keywords(text or "", _SECTION_KEYWORDS[section_lower])


def extract_section(ref: ReferenceEntry, section: str) -> str | None:
    """从 ``ref`` 抽取指定段落；找不到返回 None。

    流程：
    1. ``abstract_sections`` 精确命中 ``section`` 键（小写比较）；
    2. ``abstract`` 启发式切片（按 ``_SECTION_KEYWORDS`` 匹配）；
    3. 都失败则返回 None。
    """
    section_lower = (section or "").lower().strip()
    # 1) 结构化命中。
    if ref.abstract_sections:
        for key, val in ref.abstract_sections.items():
            if key.lower() == section_lower and val:
                return val
    # 2) 启发式切片。
    return slice_section(ref.abstract or "", section_lower)


class PaperSectionTool:
    """``fetch_paper_section``：按 reference_id + section 取段落（只读）。

    与 ``WorkspaceReadTools.read_reference`` 不同：后者返回整条元数据（title +
    abstract 整段），本工具按需取**段落**，避免把整段 abstract 一次塞入 prompt
    时模型只能做表层同义改写。

    Args:
        workspace: 持有引用以读取最新的 verified_references；不写工作区。
        max_chars: 单次返回的最大字符数（默认 1500）。
    """

    def __init__(
        self,
        workspace: PaperWorkspace,
        max_chars: int = _DEFAULT_MAX_CHARS,
    ) -> None:
        self._ws = workspace
        self._max_chars = max(1, max_chars)

    def fetch_paper_section(
        self,
        reference_id: str,
        section: str,
        max_chars: int | None = None,
    ) -> str:
        """按 reference_id + section 名取段落。

        命中：返回 ``[id] section: ...``（截断到 max_chars）。
        未命中：返回明确错误字符串并提示可用 section 集合，不抛异常。
        """
        rid = (reference_id or "").strip()
        section_name = (section or "").strip()
        cap = max(1, max_chars) if max_chars else self._max_chars

        if not rid:
            return "错误：fetch_paper_section 需要非空的 reference_id。"
        if not section_name:
            return "错误：fetch_paper_section 需要非空的 section（如 method / results）。"

        ref = self._lookup(rid)
        if ref is None:
            available = [r.id for r in self._ws.verified_references]
            hint = (
                "（当前无任何已验证文献）" if not available
                else f"（可选 reference_id：{', '.join(available[:10])}）"
            )
            return f"错误：未找到 reference_id 为 '{rid}' 的已验证文献。{hint}"

        extracted = extract_section(ref, section_name)
        if extracted is None:
            available_sections = self._available_sections(ref)
            return (
                f"错误：文献 '{rid}' 未提供 section '{section_name}'。"
                f"可用 section：{available_sections}"
            )

        snippet = extracted
        if len(snippet) > cap:
            snippet = snippet[:cap] + f"\n[...已截断，原长 {len(extracted)} 字符]"
        return f"[{ref.id}] {section_name}:\n{snippet}"

    def _lookup(self, reference_id: str) -> ReferenceEntry | None:
        for ref in self._ws.verified_references:
            if ref.id == reference_id:
                return ref
        return None

    @staticmethod
    def _available_sections(ref: ReferenceEntry) -> str:
        """给出该文献能命中的 section 名（结构化 + 启发式可切片的）。"""
        names: list[str] = list(ref.abstract_sections.keys())
        if ref.abstract:
            for sec_name, kws in _SECTION_KEYWORDS.items():
                if any(kw.lower() in ref.abstract.lower() for kw in kws):
                    if sec_name not in names:
                        names.append(sec_name)
        return ", ".join(names) if names else "（该文献无可用 section）"


def register_paper_section_tool(
    registry: ToolRegistry, workspace: PaperWorkspace,
    max_chars: int = _DEFAULT_MAX_CHARS,
) -> PaperSectionTool:
    """把 ``fetch_paper_section`` 工具注册到 registry。返回工具实例（便于测试）。"""
    tool = PaperSectionTool(workspace, max_chars=max_chars)
    registry.register(
        name="fetch_paper_section",
        description=(
            "按已验证文献的 id 与段落名取片段（motivation / method / results / "
            "conclusion 之一，或文献结构化 abstract_sections 的键）。"
            "比 read_reference 更聚焦——避免把整段 abstract 塞入上下文。只读。"
        ),
        handler=tool.fetch_paper_section,
        parameters=FETCH_SCHEMA,
    )
    return tool


__all__ = [
    "PaperSectionTool",
    "FETCH_SCHEMA",
    "extract_section",
    "slice_section",
    "register_paper_section_tool",
]
