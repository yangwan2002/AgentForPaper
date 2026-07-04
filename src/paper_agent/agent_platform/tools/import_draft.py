"""import_draft 工具：把本地论文文件（pdf/docx/tex/md/txt）导入工作区并切分章节。

补齐对话模式的关键能力——此前 agent 无法读取用户的本地文件。复用既有
``ingestion.load_document``（PDF/DOCX/LaTeX/Markdown）与 ``split_draft_into_sections``
（按 Markdown/LaTeX 标题切分；无标题则整篇作为一个「正文」章节保留）。

导入是**加载用户自有素材**，非 LLM 生成内容，故不经反幻觉护栏；经仓储原子写入
工作区（设置初稿、大纲、章节草稿，模式置为草稿修订）。
"""

from __future__ import annotations

import os
import re

from paper_agent.agent_platform.tools.context import ToolContext
from paper_agent.ingestion import (
    DocumentLoadError,
    load_document,
    split_draft_into_sections,
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
        }
    },
    "required": ["path"],
}

_IMPORT_DESCRIPTION = (
    "把用户本地的论文文件（PDF/Word/LaTeX/Markdown）导入到工作区，并按标题切分为"
    "章节，之后即可用 locate_section/read_section 查看、用改写/润色工具修改。"
    "当用户给出论文文件路径时调用。"
)


def load_sections(path: str, asset_dir: str | None = None):
    """加载文档并切分章节，返回 (全文文本, [(section_id, title, content), ...])。

    先用共享的 Markdown/LaTeX 标题切分；若只得到单个「正文」（常见于 PDF 抽取的
    无标题层级文本），再用学术标题启发式（数字编号 / 第 N 章 / 常见章节词）兜底切分。
    """
    text = load_document(path, asset_dir=asset_dir)
    triples = split_draft_into_sections(text)
    if len(triples) <= 1:
        academic = split_academic_sections(text)
        if len(academic) > 1:
            triples = academic
    return text, triples


# 常见学术章节标题词（小写匹配，覆盖中英文）。
_SECTION_WORDS = frozenset({
    "摘要", "abstract", "引言", "绪论", "introduction", "相关工作", "related work",
    "背景", "background", "方法", "研究方法", "methodology", "method", "methods",
    "approach", "实验", "实验与分析", "实验部分", "experiments", "experiment",
    "实验结果", "结果", "results", "讨论", "discussion", "结论", "总结",
    "conclusion", "conclusions", "参考文献", "references", "致谢",
})

# 学术标题行匹配。关键防误报：编号后的标题必须**以文字开头**（``[^\W\d_]`` 即字母
# 或中文，非数字/符号），从而排除表格数字行（如 "0.856 0.894"、"18.6%"）被误当标题。
# 数字编号（"1 引言" / "3.2 特征匹配"）：层级 ≤4、每级 ≤2 位（真章节号都很小）。
_NUMBERED = re.compile(r"^(\d{1,2}(?:\.\d{1,2}){0,3})[\s、.]+([^\W\d_].{0,34})$")
# 罗马数字 / 字母编号（"I. 方法" / "II. 实验与结果分析" / "A. 实验平台"）：编号后须带
# 句点再接文字标题——排除句首的 "A novel ..." 这类正文行。
_LETTER_ROMAN = re.compile(r"^([IVXLCDM]{1,4}|[A-Z])\.\s+([^\W\d_].{0,34})$")
# 中文章节："第3章 方法" / "第一章"。
_CN_CHAPTER = re.compile(r"^第[一二三四五六七八九十百千\d]+[章节][\s：:、]*(.{0,36})$")
# 结尾若是正文标点，则该行更像句子而非标题。
_SENTENCE_END = ("。", "，", "、", "；", "：", ".", ",", ";", ":")


def _is_academic_heading(line: str) -> bool:
    """判断一行是否像学术章节标题（短、无句末标点、命中编号/章节词）。"""
    s = line.strip()
    if not s or len(s) > 40 or s.endswith(_SENTENCE_END):
        return False
    if _NUMBERED.match(s) or _LETTER_ROMAN.match(s) or _CN_CHAPTER.match(s):
        return True
    return s.lower().strip("：: .。") in _SECTION_WORDS


def split_academic_sections(text: str) -> list[tuple[str, str, str]]:
    """按学术标题启发式把文本切成 [(section_id, title, content), ...]。

    无法识别任何标题时返回单个「正文」段，保证不丢内容（与共享切分器语义一致）。
    """
    if not text or not text.strip():
        return []
    sections: list[tuple[str, str, str]] = []
    cur_title: str | None = None
    cur_body: list[str] = []

    def _flush() -> None:
        if cur_title is None:
            return
        sections.append((f"sec_{len(sections)}", cur_title, "\n".join(cur_body).strip()))

    for line in text.splitlines():
        if _is_academic_heading(line):
            _flush()
            cur_title = line.strip()
            cur_body = []
        else:
            cur_body.append(line)
    _flush()

    if not sections:
        return [("sec_0", "正文", text.strip())]
    return sections


def apply_import_mutation(
    text: str, triples: list[tuple[str, str, str]], source_path: str | None = None
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

    return _mutate


def _handle_import(ctx: ToolContext, path: str) -> str:
    if not path or not path.strip():
        return "请提供论文文件路径。"
    path = path.strip().strip('"').strip("'")
    stem = os.path.splitext(os.path.basename(path))[0]
    asset_dir = os.path.join(ctx.output_dir, f"{stem}_assets")

    try:
        text, triples = load_sections(path, asset_dir=asset_dir)
    except DocumentLoadError as exc:
        return f"读取失败：{exc}"
    except Exception as exc:  # noqa: BLE001 - 解析异常按工具失败回灌，不中止会话
        return f"读取失败：{type(exc).__name__}: {exc}"

    # 导入为用户自有素材，经仓储原子写入（不走反幻觉内容护栏）。
    ctx.repo.update(ctx.workspace, apply_import_mutation(text, triples, source_path=path))
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
        handler=lambda path: _handle_import(ctx, path),
        parameters=_IMPORT_SCHEMA,
    )


__all__ = ["register_import_draft", "load_sections", "apply_import_mutation"]
