"""import_draft 工具：把本地论文文件（pdf/docx/tex/md/txt）导入工作区并切分章节。

补齐对话模式的关键能力——此前 agent 无法读取用户的本地文件。复用既有
``ingestion.load_document``（PDF/DOCX/LaTeX/Markdown）与 ``split_draft_into_sections``
（按 Markdown/LaTeX 标题切分；无标题则整篇作为一个「正文」章节保留）。

导入是**加载用户自有素材**，非 LLM 生成内容，故不经反幻觉护栏；经仓储原子写入
工作区（设置初稿、大纲、章节草稿，模式置为草稿修订）。
"""

from __future__ import annotations

import os

from paper_agent.agent_platform.tools.context import ToolContext
from paper_agent.ingestion import (
    DocumentLoadError,
    IngestionConfirmationRequired,
    ingest_document,
    split_academic_sections,
)
from paper_agent.tools.registry import ToolRegistry
from paper_agent.workspace.models import (
    InputMode,
    OutlineNode,
    PaperWorkspace,
    SectionDraft,
)

_IMPORT_SCHEMA = {
    "type": "object",
    "properties": {
        "path": {
            "type": "string",
            "description": "本地论文文件的绝对路径，支持 pdf/docx/tex/md/txt。",
        },
        "confirm": {
            "type": "boolean",
            "default": False,
            "description": "仅在此前收到 confirmation_required 后，由用户明确确认继续时设为 true。",
        },
    },
    "required": ["path"],
}

_IMPORT_DESCRIPTION = (
    "把用户本地的论文文件（PDF/Word/LaTeX/Markdown）导入到工作区，并按标题切分为"
    "章节，之后即可用 locate_section/read_section 查看、用改写/润色工具修改。"
    "当用户给出论文文件路径时调用。"
)


def load_sections(
    path: str, asset_dir: str | None = None, *, confirm: bool = False
):
    """兼容旧调用的共享摄入包装，返回全文与统一章节切分结果。"""
    ingested = ingest_document(path, asset_dir=asset_dir, confirm=confirm)
    return ingested.text, ingested.sections


def apply_import_mutation(
    text: str,
    triples: list[tuple[str, str, str]],
    source_path: str | None = None,
    quality_profile: dict | None = None,
):
    """构造把导入结果写入工作区的更新意图（设初稿/大纲/章节草稿，置草稿修订模式）。

    ``source_path`` 记录用户原文件的绝对路径与扩展名到 ``ws.profile``，供保结构处理
    （如 docx 原地润色/排版）定位原文件——避免"抽文本重建导致原格式全丢"。
    """

    def _mutate(ws: PaperWorkspace) -> None:
        ws.original_draft = text
        ws.input_mode = InputMode.DRAFT_REVISION
        ws.outline = [
            OutlineNode(section_id=sid, title=title, order=i)
            for i, (sid, title, _content) in enumerate(triples)
        ]
        ws.section_drafts = {
            sid: SectionDraft(section_id=sid, title=title, content=content)
            for sid, title, content in triples
        }
        ws.draft_sections = {sid: content for sid, title, content in triples}
        if source_path:
            ws.profile["source_document_path"] = os.path.abspath(source_path)
            ws.profile["source_document_ext"] = os.path.splitext(source_path)[1].lower()
        if quality_profile is not None:
            ws.profile["ingestion_quality"] = quality_profile

    return _mutate


def _handle_import(ctx: ToolContext, path: str, confirm: bool = False) -> str:
    if not path or not path.strip():
        return "请提供论文文件路径。"
    path = path.strip().strip('"').strip("'")
    stem = os.path.splitext(os.path.basename(path))[0]
    asset_dir = os.path.join(ctx.output_dir, f"{stem}_assets")

    try:
        ingested = ingest_document(path, asset_dir=asset_dir, confirm=confirm)
        text, triples = ingested.text, ingested.sections
    except IngestionConfirmationRequired as exc:
        warnings = "；".join(exc.report.warnings)
        return (
            "confirmation_required：文档正文可读，但摄入质量需要用户确认。"
            f"原因：{warnings}。若用户明确同意，请再次调用 import_draft，"
            "并设置 confirm=true。"
        )
    except DocumentLoadError as exc:
        return f"读取失败：{exc}"
    except Exception as exc:  # noqa: BLE001 - 解析异常按工具失败回灌，不中止会话
        return f"读取失败：{type(exc).__name__}: {exc}"

    # 导入为用户自有素材，经仓储原子写入（不走反幻觉内容护栏）。
    ctx.repo.update(
        ctx.workspace,
        apply_import_mutation(
            text,
            triples,
            source_path=path,
            quality_profile=ingested.quality.to_profile(),
        ),
    )
    ctx.session.record("import_draft", path=path, sections=len(triples))

    titles = "、".join(f"{sid}《{title}》" for sid, title, _c in triples)
    return (
        f"已导入《{os.path.basename(path)}》，共 {len(triples)} 个章节：{titles}。"
        f"可用 read_section 查看某章节、rewrite_section/polish_section 修改、"
        f"add_section 补写缺失章节。"
    )


def register_import_draft(registry: ToolRegistry, ctx: ToolContext) -> None:
    registry.register(
        name="import_draft",
        description=_IMPORT_DESCRIPTION,
        handler=lambda path, confirm=False: _handle_import(ctx, path, confirm),
        parameters=_IMPORT_SCHEMA,
    )


__all__ = [
    "register_import_draft",
    "load_sections",
    "apply_import_mutation",
    "split_academic_sections",
]
