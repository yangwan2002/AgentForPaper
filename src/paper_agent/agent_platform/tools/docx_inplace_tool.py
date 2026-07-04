"""polish_docx_inplace 工具：对用户原 .docx 做**保结构**润色/排版（P0 头号风险修复）。

问题背景：默认导出路径（``export_paper`` → ``DocxExporter``）从 ``section_drafts``
**重建** docx，用户原稿的字体/样式/编号/页眉页脚/图/表/公式/脚注/批注/修订**全丢**。

本工具走 :class:`InplaceDocxPolisher` 的**保结构范式**：直接打开用户原 .docx，只重写
正文散文段落的文字（``run.text``），其余 OOXML 结构因从不 re-emit 而**逐字保留**；并
带确定性保真守卫（引用/数字恒等）与文档级结构 diff 闸（结构被破坏则整档回滚）。可选
叠加排版规格（两端对齐/行距/首行缩进等）到同一原稿，同样保留其余格式。

与 ``rewrite_section``/``polish_section`` 的区别：那些改的是工作区 ``section_drafts``、
最终经 ``export_paper`` **重建** docx（丢原格式）；本工具**不动工作区**、直接在原 docx
上就地处理（保原格式），是"用户上传 docx → 保格式润色/调排版 → 返回 docx"的正确路径。

定位：本工具**只读工作区**、产出文件（与 ``export_paper`` 同类），不经内容护栏——其
安全性由 ``polish_guards`` 保真守卫 + 结构 diff 闸保证。
"""

from __future__ import annotations

import os

from paper_agent.agent_platform.tools.context import ToolContext
from paper_agent.agent_platform.models import Typesetting
from paper_agent.docx_inplace import InplaceDocxPolisher
from paper_agent.providers.llm.base import LLMProvider
from paper_agent.tools.registry import ToolRegistry

_SCHEMA = {
    "type": "object",
    "properties": {
        "path": {
            "type": "string",
            "description": (
                "原 .docx 文件的绝对路径；省略则用本会话已导入的原文件。"
            ),
        },
        "polish_language": {
            "type": "boolean",
            "description": (
                "是否做保结构语言润色（逐段改进表达、保留格式与事实）。默认 false，"
                "即只做保结构处理/排版，不改文字。"
            ),
        },
    },
    "required": [],
}

_DESCRIPTION = (
    "对用户的原 .docx 做**保结构**处理并产出新 docx：保留原字体/样式/编号/页眉页脚/"
    "图/表/公式/脚注/修订等一切格式，只按需润色正文文字或应用排版规格（用 "
    "set_typesetting 设定的两端对齐/行距/缩进会被套用）。当用户提供 .docx 且要求"
    "**保留原格式**做润色或调排版时，用本工具，不要用 import_draft+rewrite/polish+"
    "export_paper（那会重建 docx、丢失原格式）。"
)


def _resolve_source_docx(ctx: ToolContext, path: str | None) -> str | None:
    """解析原 docx 路径：显式参数优先，否则用导入时记录的源文件（须为 .docx）。"""
    candidate = (path or "").strip().strip('"').strip("'")
    if not candidate:
        profile = getattr(ctx.workspace, "profile", None) or {}
        if profile.get("source_document_ext") == ".docx":
            candidate = profile.get("source_document_path", "")
    if not candidate:
        return None
    if not candidate.lower().endswith(".docx") or not os.path.isfile(candidate):
        return None
    return candidate


def _saved_typesetting(ctx: ToolContext) -> Typesetting | None:
    """取工作区已保存的排版规格（set_typesetting 落定的）；无则 None。"""
    profile = getattr(ctx.workspace, "profile", None) or {}
    spec_data = profile.get("typesetting")
    if not spec_data:
        return None
    spec = Typesetting.from_dict(spec_data)
    return None if spec.is_empty() else spec


def _handle_polish_docx_inplace(
    ctx: ToolContext, llm: LLMProvider, path: str | None = None,
    polish_language: bool = False,
) -> str:
    src = _resolve_source_docx(ctx, path)
    if src is None:
        return (
            "未找到可保结构处理的原 .docx：请提供 .docx 文件的绝对路径，或先用 "
            "import_draft 导入一个 .docx 初稿。"
        )

    stem = os.path.splitext(os.path.basename(src))[0]
    os.makedirs(ctx.output_dir, exist_ok=True)
    out_path = os.path.join(ctx.output_dir, f"{stem}_inplace.docx")

    notes: list[str] = []
    try:
        if polish_language:
            result = InplaceDocxPolisher(llm).polish(src, out_path)
            notes.extend(result.notes)
            if result.rolled_back:
                notes.append("（语言润色触发结构回滚，已保留原文结构与文字。）")
        else:
            # 不润色文字：先保结构复制原稿，再（可选）套排版。
            import shutil

            shutil.copyfile(src, out_path)
            notes.append("已保结构复制原稿（未改文字）。")
    except RuntimeError as exc:
        return f"保结构处理失败：{exc}"
    except Exception as exc:  # noqa: BLE001 - 处理异常按工具失败回灌
        return f"保结构处理失败：{type(exc).__name__}: {exc}"

    # 叠加排版规格（若用户经 set_typesetting 设定）——同样作用于保结构后的原稿。
    spec = _saved_typesetting(ctx)
    if spec is not None:
        try:
            from paper_agent.export.typesetting import apply_typesetting

            apply_typesetting(out_path, spec)
            notes.append("已套用已保存的排版规格（两端对齐/行距/缩进等）。")
        except Exception as exc:  # noqa: BLE001 - 排版应用失败不影响保结构产物
            notes.append(f"（排版应用失败：{exc}）")

    ctx.session.record("polish_docx_inplace", source=src, files=[out_path])
    return f"已保结构处理并导出：{out_path}。" + " ".join(notes)


def register_polish_docx_inplace(
    registry: ToolRegistry, ctx: ToolContext, llm: LLMProvider
) -> None:
    """把 polish_docx_inplace 工具注册进 registry。"""
    registry.register(
        name="polish_docx_inplace",
        description=_DESCRIPTION,
        handler=lambda path=None, polish_language=False: _handle_polish_docx_inplace(
            ctx, llm, path, polish_language
        ),
        parameters=_SCHEMA,
    )


__all__ = ["register_polish_docx_inplace"]
