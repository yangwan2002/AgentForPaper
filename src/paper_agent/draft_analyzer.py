"""初稿缺口扫描：为「一次性澄清」提供确定性输入。

此前澄清阶段只检测「缺常规章节」一个维度，且一步一停地问。本模块把稿件初查扩展
为**确定性多维度扫描**，产出一份 ``DraftGaps`` 报告，供编排器在澄清阶段一次性收集
成一批 ``Question``、经 ``Elicitor.ask_batch`` 一屏问完——用户填一次表就够。

设计原则：
- **纯逻辑、无 I/O、无 LLM、不写工作区**——单测友好、不污染调用方状态。
- **确定性触发**：只在确有缺口时返回非空项，使非交互下 ``ask_batch`` 全取默认、
  行为逐字节不变（向后兼容）。
- **保守默认**：所有缺口问题的 default 都取「最小动作」（只润色 / 保留 / 相信原文），
  非交互管线不会因此擅自补章节或删引用。

缺口维度（目前覆盖）：
1. ``missing_sections`` —— 缺 Introduction/Related Work/Conclusion 等常规章节。
2. ``missing_reference_list`` —— 正文出现 ``[id]`` 但末尾无「参考文献 / References」段。
3. ``numeric_claims_without_artifact`` —— 正文含数字声明（F1=0.87、+3.2% 等）但
   ``ws.artifact`` 为空（无实验数据可核验）。
4. ``output_format_mismatch`` —— 输入文件类型与配置的输出格式不一致（如 .tex 输入
   却选了 docx 输出，会丢 LaTeX 结构）。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from paper_agent.prompts.section_types import SectionType, infer_section_type
from paper_agent.workspace.models import InputMode, OutputFormat, PaperWorkspace

# 视为「常规必备」的章节体裁——缺失时值得询问用户是否补齐。
# 与 ``clarification._CANONICAL`` 保持一致；此处本地副本以打破循环导入
# （clarification 反向依赖本模块的 ``DraftGaps``）。
_CANONICAL: list[tuple[SectionType, str]] = [
    (SectionType.INTRODUCTION, "引言"),
    (SectionType.RELATED_WORK, "相关工作"),
    (SectionType.CONCLUSION, "结论"),
]

# 视为「参考文献段」的标题（子串、忽略大小写 / 中文）。
_REFERENCE_HEADINGS = (
    "references", "reference", "bibliography",
    "参考文献", "引用文献", "文献",
)

# 正文里 `[id]` 形式的引用标注（兼容 [1] / [Smith2020] / [1,2]）。
_TEXT_CITATION = re.compile(r"\[([A-Za-z0-9_.:\-]+(?:\s*,\s*[A-Za-z0-9_.:\-]+)*)\]")

# 数字声明：F1=0.87 / +3.2% / 95.4% / p<0.01 这类带百分号/等号/p 值的强信号。
_NUMERIC_CLAIM = re.compile(
    r"(?:[A-Za-z][A-Za-z0-9+]*\s*[=<>]\s*\d+(?:\.\d+)?)"  # F1=0.87 / acc<0.9
    r"|\d+(?:\.\d+)?\s*%"                                   # 95.4%
    r"|p\s*[<=>]\s*0?\.\d+"                                 # p<0.01
)


@dataclass
class DraftGaps:
    """初稿缺口扫描报告（供澄清阶段构造一批 Question）。"""

    missing_sections: list[tuple[str, str]] = field(default_factory=list)
    """``[(section_type_value, 中文名), ...]``——缺常规章节。"""

    missing_reference_list: bool = False
    """正文有 `[id]` 但末尾无参考文献段。"""

    numeric_claims_without_artifact: bool = False
    """正文含数字声明但工作区无 artifact（无法核验数字真实性）。"""

    output_format_mismatch: bool = False
    """输入文件类型与配置输出格式不一致（会丢原排版）。"""

    output_format_hint: str = ""
    """输出格式冲突时的人类可读说明。"""

    def any_gap(self) -> bool:
        """是否存在任何缺口——无缺口时澄清阶段直接跳过、不向用户提问。"""
        return bool(
            self.missing_sections
            or self.missing_reference_list
            or self.numeric_claims_without_artifact
            or self.output_format_mismatch
        )


def analyze_text(
    text: str,
    *,
    titles: list[tuple[str, str]] | None = None,
    has_artifact: bool = True,
    input_ext: str = "",
    output_format: "OutputFormat | None" = None,
    check_sections: bool = True,
) -> DraftGaps:
    """文本级缺口扫描（不依赖 ``PaperWorkspace``）。

    in-place 路径没有 workspace（LaTeX/DOCX 直接读源文件），用这个入口扫描源文本。
    orchestrator 的 ``analyze_draft`` 也委托到这里，避免逻辑重复。

    Args:
        text: 初稿正文文本。
        titles: ``[(section_id, title), ...]``——已识别的章节；``None`` 表示不做
            章节检测（如 GENERATION 模式大纲尚未生成）。
        has_artifact: 是否有真实研究内容（无则数字声明会被标记为待核验）。
        input_ext: 输入文件扩展名（用于输出格式冲突检测）。
        output_format: 配置的输出格式（与 ``input_ext`` 比对）。
        check_sections: 是否做章节缺口检测（``False`` 时跳过，适用于无章节信息场景）。
    """
    gaps = DraftGaps()

    # 1) 缺常规章节。
    if check_sections:
        present: set[SectionType] = {
            infer_section_type(sid, title) for (sid, title) in (titles or [])
        }
        gaps.missing_sections = [
            (st.value, name) for (st, name) in _CANONICAL if st not in present
        ]

    # 2) 正文有 `[id]` 但末尾无参考文献段。
    has_text_citations = bool(_TEXT_CITATION.search(text))
    has_reference_heading = _has_reference_heading(text)
    if has_text_citations and not has_reference_heading:
        gaps.missing_reference_list = True

    # 3) 正文含数字声明但无 artifact。
    has_numeric_claim = bool(_NUMERIC_CLAIM.search(text))
    if has_numeric_claim and not has_artifact:
        gaps.numeric_claims_without_artifact = True

    # 4) 输入文件类型与输出格式不一致（会丢原排版）。
    if input_ext and output_format is not None:
        gaps.output_format_mismatch, gaps.output_format_hint = _check_output_mismatch(
            input_ext, output_format
        )

    return gaps


def analyze_draft(ws: PaperWorkspace, *, input_ext: str = "") -> DraftGaps:
    """扫描工作区代表的初稿，产出 ``DraftGaps``（委托给 ``analyze_text``）。

    - ``missing_sections``：仅 DRAFT_REVISION 模式检测（GENERATION 模式大纲由 plan
      生成、不视为缺口）。
    - ``missing_reference_list`` / ``numeric_claims_without_artifact`` / ``output_format_mismatch``：
      见 ``analyze_text``。

    纯函数：不修改 ``ws``，不调用 LLM，不写工作区。
    """
    return analyze_text(
        ws.original_draft or "",
        titles=(
            [(n.section_id, n.title) for n in ws.ordered_sections()]
            if ws.input_mode is InputMode.DRAFT_REVISION
            else None
        ),
        has_artifact=not (ws.artifact is None or ws.artifact.is_empty()),
        input_ext=input_ext,
        output_format=ws.output_format,
        check_sections=ws.input_mode is InputMode.DRAFT_REVISION,
    )


def _has_reference_heading(text: str) -> bool:
    """文本中是否出现「参考文献 / References」等标题（任意一行单独成段）。"""
    for line in text.splitlines():
        low = line.strip().lower().lstrip("#").strip()
        if not low:
            continue
        if any(h in low for h in _REFERENCE_HEADINGS):
            return True
    return False


def _check_output_mismatch(
    input_ext: str, output_format: OutputFormat
) -> tuple[bool, str]:
    """输入扩展名与输出格式是否一致；不一致即丢原排版风险。"""
    ext = input_ext.lower()
    latex_in = ext in (".tex", ".latex")
    docx_in = ext == ".docx"
    md_in = ext in (".md", ".markdown", ".txt", ".text")

    if latex_in and output_format is not OutputFormat.LATEX:
        return True, (
            f"初稿是 LaTeX（{ext}），但输出格式设为 {output_format.value}——"
            "转格式会丢失 preamble/公式/宏/图表环境。建议保 LaTeX 输出。"
        )
    if docx_in and output_format is not OutputFormat.DOCX:
        return True, (
            f"初稿是 Word（{ext}），但输出格式设为 {output_format.value}——"
            "转格式会丢失字体/样式/编号/页眉页脚。建议保 docx 输出。"
        )
    if md_in and output_format not in (OutputFormat.MARKDOWN, OutputFormat.LATEX, OutputFormat.DOCX):
        return True, f"初稿是文本（{ext}），输出格式 {output_format.value} 未知。"
    return False, ""


__all__ = ["DraftGaps", "analyze_draft", "analyze_text"]
