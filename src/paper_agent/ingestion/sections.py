"""所有入口共享的论文章节切分。"""

from __future__ import annotations

import re


# Markdown 标题仍按各级标题切分；LaTeX 仅 chapter/section 是工作区顶级章节，
# subsection/subsubsection 原样留在父章节正文中。
_TOP_LEVEL_HEADING = re.compile(
    r"^(?:"
    r"(?P<md>#{1,6})\s+(?P<md_title>.+?)"
    r"|"
    r"\\(?P<tex_level>chapter|section)\*?\{(?P<tex_title>[^}]+)\}"
    r")\s*$"
)

_SECTION_WORDS = frozenset(
    {
        "摘要", "abstract", "引言", "绪论", "introduction", "相关工作", "related work",
        "背景", "background", "方法", "研究方法", "methodology", "method", "methods",
        "approach", "实验", "实验与分析", "实验部分", "experiments", "experiment",
        "实验结果", "结果", "results", "讨论", "discussion", "结论", "总结",
        "conclusion", "conclusions", "参考文献", "references", "致谢",
    }
)
# Only top-level integer/Roman headings become Workspace sections. Decimal
# headings (2.1) and lettered headings (A.) stay inside their parent section.
_NUMBERED = re.compile(r"^(\d{1,2})[\s、.]+([^\W\d_].{0,34})$")
_LETTER_ROMAN = re.compile(r"^([IVXLCDM]{1,4})\.\s+([^\W\d_].{0,34})$")
_CN_CHAPTER = re.compile(r"^第[一二三四五六七八九十百千\d]+[章节][\s：:、]*(.{0,36})$")
_SENTENCE_END = ("。", "，", "、", "；", "：", ".", ",", ";", ":")


def split_draft_into_sections(draft: str) -> list[tuple[str, str, str]]:
    """按显式 Markdown/LaTeX 顶级标题切分，不丢弃标题前内容。"""
    if not draft or not draft.strip():
        return []
    sections: list[tuple[str, str, str]] = []
    cur_title: str | None = None
    cur_body: list[str] = []
    preamble: list[str] = []

    def _flush() -> None:
        if cur_title is not None:
            sections.append(
                (f"sec_{len(sections)}", cur_title, "\n".join(cur_body).strip())
            )

    for line in draft.splitlines():
        match = _TOP_LEVEL_HEADING.match(line.strip())
        if match:
            _flush()
            cur_title = (
                match.group("md_title") or match.group("tex_title") or ""
            ).strip()
            cur_body = preamble if not sections and preamble else []
            preamble = []
        elif cur_title is None:
            preamble.append(line)
        else:
            cur_body.append(line)
    _flush()

    if not sections:
        return [("sec_0", "正文", draft.strip())]
    return sections


def _is_academic_heading(line: str) -> bool:
    s = line.strip()
    if not s or len(s) > 40 or s.endswith(_SENTENCE_END):
        return False
    if _NUMBERED.match(s) or _LETTER_ROMAN.match(s) or _CN_CHAPTER.match(s):
        return True
    return s.lower().strip("：: .。") in _SECTION_WORDS


def split_academic_sections(text: str) -> list[tuple[str, str, str]]:
    """用编号、中文章名和常见学术章节词切分抽取后的无层级文本。"""
    if not text or not text.strip():
        return []
    sections: list[tuple[str, str, str]] = []
    cur_title: str | None = None
    cur_body: list[str] = []
    preamble: list[str] = []

    def _flush() -> None:
        if cur_title is not None and any(line.strip() for line in cur_body):
            sections.append(
                (f"sec_{len(sections)}", cur_title, "\n".join(cur_body).strip())
            )

    for line in text.splitlines():
        if _is_academic_heading(line):
            _flush()
            cur_title = line.strip()
            cur_body = preamble if not sections and preamble else []
            preamble = []
        elif cur_title is None:
            preamble.append(line)
        else:
            cur_body.append(line)
    _flush()

    if not sections:
        return [("sec_0", "正文", text.strip())]
    return sections


def split_document_sections(text: str) -> list[tuple[str, str, str]]:
    """统一章节入口：显式结构优先，单章节时用学术启发式兜底。"""
    sections = split_draft_into_sections(text)
    if len(sections) <= 1:
        academic = split_academic_sections(text)
        if len(academic) > 1:
            return academic
    return sections


__all__ = [
    "split_academic_sections",
    "split_document_sections",
    "split_draft_into_sections",
]
