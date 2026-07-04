"""InplacePolishWorkflow：保结构润色的确定性工作流（intent-routing-and-workflows · Task 4）。

固定步骤（写死、不经 LLM 编排）= 按源扩展名分派 → 调既有保结构润色能力（.docx 走
``InplaceDocxPolisher``、.tex 走 ``InplaceLatexPolisher``，二者已带守卫/结构 diff 闸/回滚）
→ 产出新文件、原稿只读。工作流只负责"选对处理器 + 诚实上报"，不改润色算法本身。

复用既有 ``polish_docx_inplace`` / ``polish_latex_inplace`` 工具处理函数（它们已封装
路径解析、保结构处理、排版叠加与 ``session.record``），避免重写。成功与否由处理函数是否
向 transcript 追加带 ``files`` 的记录判定（与"交付即停"的判定口径一致）。

参数（来自 :class:`~paper_agent.agent_platform.routing.RouteDecision` 的 ``params``）：
- ``source_path``：源文件绝对路径（缺省用会话已导入的原文件）。
- ``polish_language``：仅对 docx 有效——是否做语言润色（缺省 True，保结构润色语义）。
"""

from __future__ import annotations

import os

from paper_agent.agent_platform.routing import Intent
from paper_agent.agent_platform.tools.context import ToolContext
from paper_agent.agent_platform.tools.docx_inplace_tool import (
    _handle_polish_docx_inplace,
)
from paper_agent.agent_platform.tools.latex_inplace_tool import (
    _handle_polish_latex_inplace,
)
from paper_agent.agent_platform.workflows.base import WorkflowResult
from paper_agent.providers.llm.base import LLMProvider

_DOCX_EXTS = (".docx",)
_LATEX_EXTS = (".tex", ".latex")


def _resolve_ext(ctx: ToolContext, source_path: str | None) -> tuple[str | None, str]:
    """解析源文件路径与扩展名；显式参数优先，否则用 profile 记录的原文件。"""
    candidate = (source_path or "").strip().strip('"').strip("'")
    if not candidate:
        profile = getattr(ctx.workspace, "profile", None) or {}
        candidate = profile.get("source_document_path", "")
    if not candidate:
        return None, ""
    return candidate, os.path.splitext(candidate)[1].lower()


class InplacePolishWorkflow:
    """保结构润色工作流（``Intent.INPLACE_POLISH``）：按扩展名分派 docx / latex 处理器。"""

    intent = Intent.INPLACE_POLISH

    def __init__(self, llm: LLMProvider) -> None:
        self._llm = llm

    def run(self, ctx: ToolContext, params: dict) -> WorkflowResult:
        params = params or {}
        src, ext = _resolve_ext(ctx, params.get("source_path"))
        if src is None:
            return WorkflowResult(
                ok=False,
                unresolved=[
                    "未找到可保结构润色的原文件：请提供 .docx/.tex 的绝对路径，"
                    "或先用 import_draft 导入初稿。"
                ],
            )
        if ext not in _DOCX_EXTS + _LATEX_EXTS:
            return WorkflowResult(
                ok=False,
                unresolved=[f"保结构润色仅支持 .docx/.tex，收到 {ext or '未知类型'}。"],
            )

        # 记录调用前 transcript 长度，用于判定处理器是否产出文件（成功标志）。
        before = len(ctx.session.transcript)
        if ext in _DOCX_EXTS:
            polish_language = bool(params.get("polish_language", True))
            text = _handle_polish_docx_inplace(
                ctx, self._llm, path=src, polish_language=polish_language
            )
        else:
            text = _handle_polish_latex_inplace(ctx, self._llm, path=src)

        files = _produced_files(ctx, before)
        if not files:
            # 处理器未产出文件 → 失败，把其返回文本作为原因诚实上报。
            return WorkflowResult(ok=False, unresolved=[text])
        return WorkflowResult(ok=True, files=files, notes=[text])


def _produced_files(ctx: ToolContext, before: int) -> list[str]:
    """扫描调用后新增的 transcript 记录，收集其中的产物文件路径。"""
    files: list[str] = []
    for entry in ctx.session.transcript[before:]:
        for path in entry.get("files", []) or []:
            if path not in files:
                files.append(path)
    return files


__all__ = ["InplacePolishWorkflow"]
