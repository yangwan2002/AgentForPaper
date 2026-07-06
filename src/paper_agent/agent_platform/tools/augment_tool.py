"""augment_document 工具：在用户原 .docx/.tex 上**就地增补**新章节 / 参考文献（保结构）。

问题背景：给一份成品稿要「补引言、加参考文献并保留原格式」时，走 import_draft + add_section +
export_paper 的重建路会**丢公式（docx 的 OMML）、乱表格、重复参考文献**。本工具走
:class:`~paper_agent.inplace_augment.InplaceDocxAugmenter` / :class:`InplaceLatexAugmenter` 的
**只增不改**范式：直接在原文件插入新内容，原有公式/表格/格式逐字保留，参考文献只插唯一一份。

定位：本工具**只读工作区**、产出新文件（与 export_paper / convert_document 同类），其安全性由
增补器的 Preservation_Check（结构无损校验）+ 失败保留原稿保证。
"""

from __future__ import annotations

import os

from paper_agent.agent_platform.tools.context import ToolContext
from paper_agent.export.atomic_write import atomic_write_text
from paper_agent.inplace_augment import (
    InplaceDocxAugmenter,
    InplaceLatexAugmenter,
    SectionSpec,
)
from paper_agent.tools.registry import ToolRegistry

_DOCX_EXTS = (".docx",)
_LATEX_EXTS = (".tex", ".latex")

_SCHEMA = {
    "type": "object",
    "properties": {
        "sections": {
            "type": "array",
            "description": "要新增的章节列表（每个含 title 与正文 body）。",
            "items": {
                "type": "object",
                "properties": {
                    "title": {"type": "string", "description": "章节标题，如「引言」。"},
                    "body": {"type": "string", "description": "章节正文（纯文本/Markdown）。"},
                    "position": {
                        "type": "string",
                        "enum": ["start", "end"],
                        "description": "插到正文开头(start，默认)或末尾(end)。",
                    },
                },
                "required": ["title"],
            },
        },
        "references": {
            "type": "array",
            "items": {"type": "string"},
            "description": "要在文末追加的参考文献条目（每条一整行，已格式化好）。",
        },
        "path": {
            "type": "string",
            "description": "原 .docx/.tex 文件绝对路径；省略则用本会话已导入的原文件。",
        },
    },
    "required": [],
}

_DESCRIPTION = (
    "在用户的原 .docx/.tex 上**就地增补**新章节（如引言）与参考文献并产出新文件，"
    "**保留原稿的公式/表格/字体/编号/preamble 等一切格式**（只新增、不重建）。当用户给"
    "成品稿且要「补写章节 / 在文末加参考文献并保留原格式」时用本工具；不要用 import_draft"
    "+ add_section + export_paper（那会重建、丢公式、重复参考文献）。参考文献只会插入唯一一份。"
)


def _resolve_source(ctx: ToolContext, path: str | None) -> tuple[str | None, str]:
    """解析原文件路径与扩展名；显式参数优先，否则用 profile 记录的源文件。"""
    candidate = (path or "").strip().strip('"').strip("'")
    if not candidate:
        profile = getattr(ctx.workspace, "profile", None) or {}
        candidate = profile.get("source_document_path", "")
    if not candidate or not os.path.isfile(candidate):
        return None, ""
    return candidate, os.path.splitext(candidate)[1].lower()


def _parse_sections(sections) -> list[SectionSpec]:
    specs: list[SectionSpec] = []
    for item in sections or []:
        if not isinstance(item, dict):
            continue
        title = str(item.get("title", "")).strip()
        if not title:
            continue
        specs.append(
            SectionSpec(
                title=title,
                body=str(item.get("body", "") or ""),
                position=str(item.get("position", "start") or "start"),
            )
        )
    return specs


def _handle_augment(
    ctx: ToolContext, sections=None, references=None, path: str | None = None
) -> str:
    src, ext = _resolve_source(ctx, path)
    if src is None:
        return (
            "未找到可增补的原文件：请提供 .docx/.tex 的绝对路径，或先用 import_draft 导入初稿。"
        )
    specs = _parse_sections(sections)
    refs = [str(r) for r in (references or []) if str(r).strip()]
    if not specs and not refs:
        return "未提供要增补的章节或参考文献，未做变更。"

    stem = os.path.splitext(os.path.basename(src))[0]
    os.makedirs(ctx.output_dir, exist_ok=True)

    if ext in _DOCX_EXTS:
        out_path = os.path.join(ctx.output_dir, f"{stem}_augmented.docx")
        try:
            result = InplaceDocxAugmenter().augment(
                src, out_path, sections=specs, references=refs
            )
        except Exception as exc:  # noqa: BLE001 - 增补异常按工具失败回灌
            return f"就地增补失败：{type(exc).__name__}: {exc}"
    elif ext in _LATEX_EXTS:
        try:
            with open(src, encoding="utf-8", errors="replace") as fh:
                source = fh.read()
            new_source, result = InplaceLatexAugmenter().augment(
                source, sections=specs, references=refs
            )
            out_path = os.path.join(ctx.output_dir, f"{stem}_augmented.tex")
            if result.ok:
                atomic_write_text(out_path, new_source)
                result.out_path = out_path
        except Exception as exc:  # noqa: BLE001 - 增补异常按工具失败回灌
            return f"就地增补失败：{type(exc).__name__}: {exc}"
    else:
        return f"就地增补仅支持 .docx/.tex，收到 {ext or '未知类型'}。"

    if not result.ok:
        return f"就地增补未完成（已保留原稿）：{result.error}"

    ctx.session.record(
        "augment_document", source=src, files=[result.out_path or out_path]
    )
    note = ""
    if result.notes:
        note = "（" + "；".join(result.notes) + "）"
    return (
        f"已就地增补并导出：{result.out_path or out_path}"
        f"（新增章节 {result.inserted_sections}，参考文献 {result.inserted_references} 条；"
        f"原稿公式/表格/格式逐字保留）。{note}"
    )


def register_augment_document(registry: ToolRegistry, ctx: ToolContext) -> None:
    """把 augment_document 工具注册进 registry。"""
    registry.register(
        name="augment_document",
        description=_DESCRIPTION,
        handler=lambda sections=None, references=None, path=None: _handle_augment(
            ctx, sections, references, path
        ),
        parameters=_SCHEMA,
    )


__all__ = ["register_augment_document"]
