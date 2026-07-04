"""polish_latex_inplace 工具：对用户原 .tex 做**保结构**语言润色（与 docx 对称）。

问题背景（与 docx 同源）：默认导出路径从 ``section_drafts`` **重建** `.tex`，用户原稿的
preamble、``\\newcommand`` 宏、``\\input`` 包含、公式/表格/图的精细排版可能丢失或被简化。

本工具走 :class:`InplaceLatexPolisher` 的**保结构范式**：把用户的 LaTeX 源**当作真相**，
只润色其中的自然语言散文，**逐字节保留** preamble/宏/数学/环境/``\\cite``/``\\ref``/
``\\label``/图表/注释与整体结构；并带确定性守卫（命令多重集合/括号计数/数字/引用集合恒等，
长度受限），任一破坏即丢弃该片段润色、保留原文。往返对结构无损。

与 ``rewrite_section``/``polish_section`` 的区别：那些改工作区 ``section_drafts``、最终经
``export_paper`` **重建** `.tex`（丢原结构）；本工具**不动工作区**、直接在原 `.tex` 上就地
润色（保原结构），是"用户上传 tex → 保结构润色语言 → 返回 tex"的正确路径。

范围说明：本工具**只润色语言**，不新增引用（新增 ``\\cite`` 会破坏守卫的命令集合恒等）。
要加文献请走 add_references + 重建导出；保原结构改语言用本工具。
"""

from __future__ import annotations

import os

from paper_agent.agent_platform.tools.context import ToolContext
from paper_agent.export.atomic_write import atomic_write_text
from paper_agent.latex_inplace import InplaceLatexPolisher
from paper_agent.providers.llm.base import LLMProvider
from paper_agent.tools.registry import ToolRegistry

_SCHEMA = {
    "type": "object",
    "properties": {
        "path": {
            "type": "string",
            "description": "原 .tex 文件的绝对路径；省略则用本会话已导入的原文件。",
        }
    },
    "required": [],
}

_DESCRIPTION = (
    "对用户的原 .tex 做**保结构**语言润色并产出新 .tex：逐字保留 preamble/宏/公式/环境/"
    "图表/引用/注释等一切结构，只改进正文散文的语言表达。当用户提供 .tex 且要求**保留原"
    "结构**做润色时，用本工具，不要用 import_draft+rewrite/polish+export_paper（那会从文本"
    "重建 .tex、丢失 preamble 与宏）。注意：本工具不新增引用（要加文献走 add_references）。"
)


def _resolve_source_tex(ctx: ToolContext, path: str | None) -> str | None:
    """解析原 .tex 路径：显式参数优先，否则用导入时记录的源文件（须为 .tex/.latex）。"""
    candidate = (path or "").strip().strip('"').strip("'")
    if not candidate:
        profile = getattr(ctx.workspace, "profile", None) or {}
        if profile.get("source_document_ext") in (".tex", ".latex"):
            candidate = profile.get("source_document_path", "")
    if not candidate:
        return None
    if not candidate.lower().endswith((".tex", ".latex")) or not os.path.isfile(candidate):
        return None
    return candidate


def _handle_polish_latex_inplace(
    ctx: ToolContext, llm: LLMProvider, path: str | None = None
) -> str:
    src = _resolve_source_tex(ctx, path)
    if src is None:
        return (
            "未找到可保结构处理的原 .tex：请提供 .tex 文件的绝对路径，或先用 "
            "import_draft 导入一个 .tex 初稿。"
        )

    try:
        with open(src, encoding="utf-8", errors="replace") as fh:
            source = fh.read()
    except Exception as exc:  # noqa: BLE001 - 读文件失败按工具失败回灌
        return f"读取原 .tex 失败：{type(exc).__name__}: {exc}"

    try:
        result = InplaceLatexPolisher(llm).polish(source)
    except Exception as exc:  # noqa: BLE001 - 润色异常按工具失败回灌
        return f"保结构润色失败：{type(exc).__name__}: {exc}"

    stem = os.path.splitext(os.path.basename(src))[0]
    os.makedirs(ctx.output_dir, exist_ok=True)
    out_path = os.path.join(ctx.output_dir, f"{stem}_inplace.tex")
    try:
        atomic_write_text(out_path, result.source)
    except Exception as exc:  # noqa: BLE001 - 落盘失败按工具失败回灌
        return f"写出润色后的 .tex 失败：{type(exc).__name__}: {exc}"

    ctx.session.record("polish_latex_inplace", source=src, files=[out_path])
    return f"已保结构润色并导出：{out_path}。" + " ".join(result.notes)


def register_polish_latex_inplace(
    registry: ToolRegistry, ctx: ToolContext, llm: LLMProvider
) -> None:
    """把 polish_latex_inplace 工具注册进 registry。"""
    registry.register(
        name="polish_latex_inplace",
        description=_DESCRIPTION,
        handler=lambda path=None: _handle_polish_latex_inplace(ctx, llm, path),
        parameters=_SCHEMA,
    )


__all__ = ["register_polish_latex_inplace"]
