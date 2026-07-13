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
_NUMBERED = re.compile(
    r"^(?P<num>\d{1,2})[\s、.]+(?P<title>[^\W\d_].{0,34})$"
)
_LETTER_ROMAN = re.compile(
    r"^(?P<roman>[IVXLCDM]{1,4})\.\s+(?P<title>[^\W\d_].{0,34})$"
)
# 单字母罗马数字 C/D/L/M 在 PDF 中更常是子节 (C.)，不是 I./II. 级章节。
_TOP_LEVEL_ROMAN = frozenset({"I", "II", "III", "IV", "V", "VI", "VII", "VIII", "IX", "X", "XI", "XII"})
_MATH_NOISE = re.compile(
    r"(?:"
    r"[=^]|"
    r"\b[kxyz]\s*[=)]|"
    r"^[xy]\s*/\s*m|"
    r"^其中"
    r")",
    re.IGNORECASE,
)
_CN_CHAPTER = re.compile(r"^第[一二三四五六七八九十百千\d]+[章节][\s：:、]*(.{0,36})$")
_SENTENCE_END = ("。", "，", "、", "；", "：", ".", ",", ";", ":")
_STANDALONE_NUMBER = re.compile(r"^\d{1,2}\.?$")
_STANDALONE_ROMAN = re.compile(r"^[IVXLCDM]{1,4}\.?$")
_PDF_HEADER_FOOTER = re.compile(
    r"^(?:"
    r"(?:https?://(?:dx\.)?doi\.org/)?10\.\d{4,}/\S+"
    r"|(?:doi|DOI)\s*:?\s*10\.\d{4,}/\S+"
    r"|(?:Vol\.?|Volume|No\.?|Issue|Iss\.?|Part)\s*[\d:.]+(?:\s*[-–—]\s*[\d:.]+)?"
    r"|Page\s+\d+\s+of\s+\d+"
    r"|第\s*\d+\s*页"
    r"|\d{3,4}\s*$"
    r")$",
    re.IGNORECASE,
)


def _heading_title_text_ok(title: str) -> bool:
    t = title.strip()
    if not t or _MATH_NOISE.search(t):
        return False
    if re.fullmatch(r"[A-Za-z]{1,4}", t) and t.upper() not in _SECTION_WORDS:
        return False
    return True


def _is_numbered_heading(line: str) -> bool:
    match = _NUMBERED.match(line.strip())
    if not match:
        return False
    if int(match.group("num")) > 12:
        return False
    return _heading_title_text_ok(match.group("title"))


def _is_roman_heading(line: str) -> bool:
    match = _LETTER_ROMAN.match(line.strip())
    if not match:
        return False
    if match.group("roman") not in _TOP_LEVEL_ROMAN:
        return False
    return _heading_title_text_ok(match.group("title"))


def _looks_like_title_line(line: str) -> bool:
    s = line.strip()
    if not s or len(s) > 40:
        return False
    if _STANDALONE_NUMBER.match(s) or _STANDALONE_ROMAN.match(s):
        return False
    if s.endswith(_SENTENCE_END):
        return False
    if not re.search(r"[^\W\d_]", s, flags=re.UNICODE):
        return False
    if s.count("。") + s.count(".") > 1:
        return False
    if _MATH_NOISE.search(s):
        return False
    return True


def _merge_split_heading_lines(lines: list[str]) -> list[str]:
    """PyMuPDF 常把「1」与「引言(Introduction)」拆成两行，合并后再切分。"""
    merged: list[str] = []
    idx = 0
    while idx < len(lines):
        current = lines[idx]
        stripped = current.strip()
        if idx + 1 < len(lines):
            nxt = lines[idx + 1].strip()
            if (
                (_STANDALONE_NUMBER.match(stripped) or _STANDALONE_ROMAN.match(stripped))
                and _looks_like_title_line(nxt)
            ):
                prefix = stripped.rstrip(".")
                merged.append(f"{prefix} {nxt}")
                idx += 2
                continue
        merged.append(current)
        idx += 1
    return merged


def _strip_pdf_header_footer_lines(lines: list[str]) -> list[str]:
    """去掉 DOI、刊头、孤立页码等不应进入正文的行。"""
    return [line for line in lines if not _PDF_HEADER_FOOTER.match(line.strip())]


def normalize_extracted_text(text: str, *, strip_pdf_noise: bool = False) -> str:
    """PDF/抽取文本预处理：可选剥离页眉页脚，并合并两行章节标题。"""
    if not text:
        return text
    lines = text.splitlines()
    lines = _merge_split_heading_lines(lines)
    if strip_pdf_noise:
        lines = _strip_pdf_header_footer_lines(lines)
    return "\n".join(lines)


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
    if _is_numbered_heading(s) or _is_roman_heading(s) or _CN_CHAPTER.match(s):
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
    text = normalize_extracted_text(text)
    sections = split_draft_into_sections(text)
    if len(sections) <= 1:
        academic = split_academic_sections(text)
        if len(academic) > 1:
            return academic
    return sections


__all__ = [
    "normalize_extracted_text",
    "split_academic_sections",
    "split_document_sections",
    "split_draft_into_sections",
]
