"""单一入口决策（CLI 瘦身的核心）。

此前 CLI 把「三条历史执行路径 + 一堆补丁开关」直接暴露给用户，逼用户判断该用哪条
pipeline、要不要交互、需不需要 artifact。本模块把这些决策收敛为**据文件类型/内容
自动决定**的纯函数，让上层 CLI 变成「给一个文件或主题 → 系统自己决定怎么处理」。

这里只放**纯决策逻辑**（无 I/O、无网络、无 LLM），便于单测；真正的执行（构造
provider、跑原地润色或 Orchestrator）由 `scripts/run_real.py` 据这些决策接线。
"""

from __future__ import annotations

import os
from enum import Enum

from paper_agent.workspace.models import OutputFormat

# 视为「初稿文件」的扩展名（其余参数按主题处理）。
_DRAFT_EXTS = frozenset(
    {".tex", ".latex", ".docx", ".md", ".markdown", ".txt", ".text", ".pdf"}
)

# 文件扩展名 → 默认输出格式（走完整管线时；输出默认=输入）。
_EXT_TO_OUTPUT = {
    ".tex": OutputFormat.LATEX,
    ".latex": OutputFormat.LATEX,
    ".docx": OutputFormat.DOCX,
    ".md": OutputFormat.MARKDOWN,
    ".markdown": OutputFormat.MARKDOWN,
    ".txt": OutputFormat.MARKDOWN,
    ".text": OutputFormat.MARKDOWN,
    ".pdf": OutputFormat.MARKDOWN,
}


class Engine(str, Enum):
    """处理引擎：据文件类型自动选择。"""

    LATEX_INPLACE = "latex_inplace"   # .tex/.latex → 保结构原地润色
    DOCX_INPLACE = "docx_inplace"     # .docx → 保结构原地润色
    PIPELINE = "pipeline"             # .md/.txt/.pdf 或主题 → 完整重渲染管线


def looks_like_draft(arg: str | None) -> bool:
    """判断一个位置参数更像「初稿文件」还是「主题」。

    命中已知初稿扩展名，或指向一个真实存在的文件，即视为初稿；否则按主题处理。
    """
    if not arg:
        return False
    ext = os.path.splitext(arg)[1].lower()
    if ext in _DRAFT_EXTS:
        return True
    return os.path.isfile(arg)


def decide_engine(draft_path: str | None, *, rebuild: bool = False) -> Engine:
    """据初稿文件类型决定处理引擎（默认「保结构」）。

    - `.tex`/`.latex` → LATEX_INPLACE（除非 ``rebuild``）；
    - `.docx` → DOCX_INPLACE（除非 ``rebuild``）；
    - 其它（`.md`/`.txt`/`.pdf` 或无初稿）→ PIPELINE。

    ``rebuild=True`` 强制一切走完整管线（会丢原排版），作为逃生舱。
    """
    if not draft_path:
        return Engine.PIPELINE
    ext = os.path.splitext(draft_path)[1].lower()
    if not rebuild and ext in (".tex", ".latex"):
        return Engine.LATEX_INPLACE
    if not rebuild and ext == ".docx":
        return Engine.DOCX_INPLACE
    return Engine.PIPELINE


def default_output_format(draft_path: str | None) -> OutputFormat:
    """走完整管线时的默认输出格式：默认等于输入文件格式；无初稿时 Markdown。"""
    if not draft_path:
        return OutputFormat.MARKDOWN
    ext = os.path.splitext(draft_path)[1].lower()
    return _EXT_TO_OUTPUT.get(ext, OutputFormat.MARKDOWN)


__all__ = [
    "Engine",
    "looks_like_draft",
    "decide_engine",
    "default_output_format",
]
