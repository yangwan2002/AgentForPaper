"""LaTeX 导出器（Req 10.4 / 10.5 / 10.6；venue-templates-figures-tables）。

产出：
- `<id>.tex`：论文正文，章节用 \\section，引用用 \\cite{key}。
- `<id>.bib`：已验证文献库导出的 BibTeX，作为唯一引用来源（Req 10.5）。

模板 / 图片 / 表格集成（venue-templates-figures-tables）：
- 前导（``\\documentclass`` + ``\\usepackage``）由 :class:`TemplateEngine` 按选定
  ``Venue_Id`` 产出的 :class:`Scaffold` 提供；``default`` 会议逐字节复现旧前导。
- 每个 :class:`FigureRecord` 若能经 :func:`safe_relative_asset` 定位到导出目录内
  且真实存在的资产，则在 ``figure`` 环境内先写 ``\\includegraphics`` 再写
  ``\\caption`` / ``\\label``；否则保留仅 ``\\caption`` / ``\\label`` 的回退并发
  ``DEGRADATION`` 事件（``missing_asset`` / ``unsafe_path``）。
- 结果表由 :class:`TableRenderer` 从 ``ws.artifact`` 渲染（grounding 不放宽）。

只引用已验证文献库中的条目（与 Req 4.3 一致）。
"""

from __future__ import annotations

import os
import re

from paper_agent.export.asset_paths import safe_relative_asset
from paper_agent.export.atomic_write import atomic_write_text
from paper_agent.export.base import ExportResult
from paper_agent.export.citation_closure import cited_references
from paper_agent.export.grounding import GroundingChecker
from paper_agent.export.inline_citations import render_inline_citations
from paper_agent.export.pandoc_pipeline import ConversionResult, PandocConverter
from paper_agent.export.table_renderer import TableRenderer
from paper_agent.export.template_engine import Scaffold, TemplateEngine
from paper_agent.export.venue_registry import VenueRegistry
from paper_agent.observability.events import Event, EventKind, NullSink
from paper_agent.workspace.models import OutputFormat, PaperWorkspace, ReferenceEntry

_SPECIAL = {
    "&": r"\&", "%": r"\%", "$": r"\$", "#": r"\#",
    "_": r"\_", "{": r"\{", "}": r"\}", "~": r"\textasciitilde{}",
    "^": r"\textasciicircum{}",
}

# 结果表浮点小数位数默认值（对齐 config.figure_float_decimals 默认）。
_DEFAULT_FLOAT_DECIMALS = 3

# pandoc 不可用 + fallback 策略时，与 DEGRADATION 事件一致的降级标注（Req 8.2）。
_PANDOC_DEGRADE_NOTE = "已降级：pandoc 不可用"

# pandoc 不可用 + fail_fast 策略时的错误说明（含安装指引，1–500 字符；Req 8.4）。
_PANDOC_FAIL_FAST_NOTE = (
    "LaTeX 导出失败：pandoc 不可用且降级策略为 fail_fast。"
    "请安装 pandoc 后重试（安装指引见 https://pandoc.org/installing.html）。"
)


def _escape(text: str) -> str:
    return "".join(_SPECIAL.get(ch, ch) for ch in text)


class _PandocSectionError(Exception):
    """某章节 pandoc 转换非零退出时抛出，用于干净地中止本次导出（Req 6.4）。

    携带失败章节标识与 :class:`ConversionResult`，供 :meth:`LatexExporter.export`
    在写任何文件之前捕获并返回带失败章节标识的错误结果。
    """

    def __init__(self, section_id: str, result: ConversionResult) -> None:
        self.section_id = section_id
        self.result = result
        super().__init__(
            f"pandoc conversion failed for section {section_id!r} "
            f"(exit_code={result.exit_code})"
        )


def _bib_key(entry: ReferenceEntry) -> str:
    """由文献生成稳定的 BibTeX 引用键。"""
    first_author = entry.authors[0].split()[-1] if entry.authors else "anon"
    year = entry.year if entry.year is not None else "n.d."
    base = re.sub(r"[^A-Za-z0-9]", "", f"{first_author}{year}")
    return base or re.sub(r"[^A-Za-z0-9]", "", entry.id) or "ref"


class LatexExporter:
    format = OutputFormat.LATEX

    def __init__(
        self,
        template_engine=None,
        table_renderer=None,
        sink=None,
        pandoc=None,
        *,
        pandoc_degrade_strategy: str = "fallback",
        pandoc_probe_timeout: float = 5.0,
    ) -> None:
        """注入模板引擎与表渲染器；均可缺省以保持 ``get_exporter()`` 无参构造兼容。

        - ``sink``：可观测事件接收器，缺省为 :class:`NullSink`。
        - ``template_engine``：缺省构造 ``TemplateEngine(VenueRegistry(), sink)``。
        - ``table_renderer``：缺省为 ``None``，在 :meth:`export` 内按 ``ws.artifact``
          现构造 ``TableRenderer(GroundingChecker(ws.artifact), sink)``（因其需要
          artifact 提供 grounding 允许集合）。
        - ``pandoc``：章节正文体的 Markdown→LaTeX 片段转换器，缺省为
          :class:`PandocConverter`；每次 :meth:`export` 探测一次可用性，可用则经
          pandoc 转换章节正文，不可用则回退至手写 ``_escape`` 渲染器。
        - ``pandoc_degrade_strategy``：pandoc 不可用时的降级策略（依赖注入，不在
          exporter 内读全局 config；Req 8.2/8.4）。``"fallback"``（默认）→ 回退手写
          渲染器并一致标注「已降级：pandoc 不可用」；``"fail_fast"`` → 以含 pandoc
          安装指引的 1–500 字符错误说明返回空产物（不写文件）。非法值按 ``"fallback"``
          处理（合法取值集合由装配层 config 校验，见 Property 27 / 任务 1.2）。
        - ``pandoc_probe_timeout``：探测 pandoc 可用性的超时秒数（Req 8.1）。
        """
        self._sink = sink if sink is not None else NullSink()
        self._template_engine = (
            template_engine
            if template_engine is not None
            else TemplateEngine(VenueRegistry(), self._sink)
        )
        self._table_renderer = table_renderer
        self._pandoc = pandoc if pandoc is not None else PandocConverter()
        self._pandoc_degrade_strategy = pandoc_degrade_strategy
        self._pandoc_probe_timeout = pandoc_probe_timeout

    def export(self, ws: PaperWorkspace, out_dir: str) -> ExportResult:
        # 每次导出探测一次 pandoc 可用性（Req 6.1/8.1）。不可用时按注入的降级策略决策：
        # - fail_fast：不写任何文件，返回带 pandoc 安装指引的错误标注（Req 8.4）。
        # - fallback（默认）：继续用手写 _escape 渲染器产出，并在末尾一致标注降级并发事件。
        pandoc_available = self._pandoc.probe(timeout=self._pandoc_probe_timeout)
        if not pandoc_available and self._pandoc_degrade_strategy == "fail_fast":
            return ExportResult(
                output_format=self.format, files=[], notes=[_PANDOC_FAIL_FAST_NOTE]
            )

        os.makedirs(out_dir, exist_ok=True)

        # Venue_Id 选择：ws.profile["venue_id"] 优先，否则 default。
        venue_id = "default"
        styles_dir = None
        try:
            profile = ws.profile or {}
            if isinstance(profile, dict) and profile.get("venue_id"):
                venue_id = str(profile["venue_id"])
            if isinstance(profile, dict) and profile.get("styles_dir"):
                styles_dir = str(profile["styles_dir"])
        except Exception:  # noqa: BLE001 - profile 异常不阻断导出
            venue_id = "default"
            styles_dir = None

        scaffold = self._template_engine.build_scaffold(
            venue_id, out_dir, styles_dir=styles_dir
        )

        # 中止分支（default 亦不可用）：不写任何文件，仅返回带说明的空结果。
        if scaffold.aborted:
            note = (
                f"已中止 LaTeX 导出：默认模板不可用"
                f"（请求 venue_id={scaffold.requested_venue_id or venue_id}）"
            )
            return ExportResult(
                output_format=self.format, files=[], notes=[note]
            )

        # 引用闭合：只处理被正文实际引用的文献（保持既定顺序，编号稳定）。
        refs = cited_references(ws)

        # 文献 → BibTeX key 映射（去重）。
        keys: dict[str, str] = {}
        used: set[str] = set()
        for ref in refs:
            key = _bib_key(ref)
            candidate, n = key, 1
            while candidate in used:
                n += 1
                candidate = f"{key}{chr(96 + n)}"
            used.add(candidate)
            keys[ref.id] = candidate

        # 表渲染器：优先使用注入实例，否则按当前 artifact 现构造。
        table_renderer = self._table_renderer
        if table_renderer is None:
            table_renderer = TableRenderer(
                GroundingChecker(ws.artifact),
                self._sink,
                float_decimals=_DEFAULT_FLOAT_DECIMALS,
            )

        figure_files: list[str] = []
        # pandoc 可用性已在入口探测（见 export 顶部）：可用则章节正文经 pandoc
        # 转换为 LaTeX 片段；不可用（fallback 策略）则回退至手写 ``_escape`` 渲染器。
        try:
            tex = self._render_tex(
                ws, keys, scaffold, out_dir, table_renderer, figure_files,
                pandoc_available,
            )
        except _PandocSectionError as exc:
            # 某章节 pandoc 转换非零退出（Req 6.4）：干净中止，不写任何文件，
            # 返回含失败章节标识的错误结果。
            note = (
                f"已中止 LaTeX 导出：章节 '{exc.section_id}' 的 pandoc 转换失败"
                f"（exit_code={exc.result.exit_code}）"
            )
            return ExportResult(
                output_format=self.format, files=[], notes=[note]
            )

        tex_path = os.path.join(out_dir, f"{ws.workspace_id}.tex")
        bib_path = os.path.join(out_dir, f"{ws.workspace_id}.bib")

        # 原子落盘：tmp-then-rename，崩溃不留半截 .tex/.bib。
        atomic_write_text(tex_path, tex)
        atomic_write_text(bib_path, self._render_bib(ws, keys))

        # 汇总真实产出文件：文档 + 样式资产 + 图像资产（去重，仅保留存在者）。
        files: list[str] = [tex_path, bib_path]
        for path in list(scaffold.asset_files) + figure_files:
            if path and path not in files and os.path.exists(path):
                files.append(path)

        notes: list[str] = []
        if scaffold.degraded and scaffold.degrade_note:
            notes.append(scaffold.degrade_note)

        # pandoc 不可用 + fallback：产出仍经手写渲染器完成，此处一致标注降级并发事件（Req 8.2）。
        if not pandoc_available:
            notes.append(_PANDOC_DEGRADE_NOTE)
            self._emit_pandoc_degradation()

        return ExportResult(output_format=self.format, files=files, notes=notes)

    def _render_tex(
        self,
        ws: PaperWorkspace,
        keys: dict[str, str],
        scaffold: Scaffold,
        out_dir: str,
        table_renderer: TableRenderer,
        figure_files: list[str],
        pandoc_available: bool,
    ) -> str:
        # 前导来自模板引擎脚手架（document_class + usepackage 行）。
        lines: list[str] = list(scaffold.preamble_lines)
        lines.append(r"\begin{document}")
        for node in ws.ordered_sections():
            draft = ws.section_drafts.get(node.section_id)
            lines.append(rf"\section{{{_escape(node.title)}}}")
            if draft:
                # 行内 [id] -> \cite{key}（#2）：用 alnum 占位符先替换，转换正文后
                # 再还原占位符，避免 \cite{} 的反斜杠/花括号被渲染破坏，也避免 id
                # 中的下划线被转义后匹配失败。alnum 占位符 pandoc 原样透传、_escape
                # 也不改动，故两条渲染路径共用此保护技巧。
                placeholders: dict[str, str] = {}
                id_to_placeholder = {}
                for rid in draft.cited_reference_ids:
                    if rid in keys and rid not in id_to_placeholder:
                        ph = f"CITEPH{len(placeholders)}"
                        id_to_placeholder[rid] = ph
                        placeholders[ph] = rf"\cite{{{keys[rid]}}}"
                content, rendered = render_inline_citations(
                    draft.content, id_to_placeholder
                )
                # 唯一被替换的一段：章节正文体渲染（Req 6.1/6.3）。
                # pandoc 可用 → 经 pandoc 把 Normalized_Markdown 正文转为 LaTeX 片段
                # （数学 $...$/$$...$$ 被正确转换而非逐字符转义）；
                # pandoc 不可用 → 回退至既有手写 _escape 渲染器（保留当前行为）。
                if pandoc_available:
                    result = self._pandoc.convert(content, target="latex")
                    if not result.ok:
                        raise _PandocSectionError(node.section_id, result)
                    body = result.content
                else:
                    body = self._render_body_fallback(content)
                for ph, tok in placeholders.items():
                    body = body.replace(ph, tok)
                lines.append(body)
                # 未在正文出现的已记录引用走章节末回退（合并为一条 \cite）。
                missing = [
                    keys[rid] for rid in draft.cited_reference_ids
                    if rid in keys and rid not in rendered
                ]
                if missing:
                    lines.append(rf"\cite{{{','.join(missing)}}}")
        # 图（Req 4.1-4.6）：有资产则先 \includegraphics 再图题/标签，否则回退。
        for fig in ws.figures:
            lines.append(r"\begin{figure}[h]")
            rel = safe_relative_asset(out_dir, fig.data_ref)
            if rel is None:
                # 路径缺失 / 非法 / 穿越导出目录：仅图题+标签回退。
                self._emit_figure_degradation(fig, "unsafe_path")
            else:
                abs_path = os.path.abspath(os.path.join(out_dir, rel))
                if os.path.exists(abs_path):
                    lines.append(rf"  \includegraphics{{{rel}}}")
                    if abs_path not in figure_files:
                        figure_files.append(abs_path)
                else:
                    # 路径安全但文件不存在：仅图题+标签回退。
                    self._emit_figure_degradation(fig, "missing_asset")
            lines.append(rf"  \caption{{{_escape(fig.caption)}}}")
            lines.append(rf"  \label{{{fig.figure_id}}}")
            lines.append(r"\end{figure}")
        # 结果表（Req 6.2）：从 artifact 渲染 table/tabular 片段。
        for fragment in table_renderer.render_latex(ws.artifact):
            lines.append(fragment.latex)
        if keys:
            lines.append(rf"\bibliography{{{ws.workspace_id}}}")
            lines.append(r"\bibliographystyle{plain}")
        lines.append(r"\end{document}")
        return "\n".join(lines)

    def _render_body_fallback(self, content: str) -> str:
        """pandoc 不可用时的章节正文回退渲染器。

        沿用既有手写逐字符转义逻辑（``_escape``）保持当前行为与既有测试不变；
        引用占位符 ``CITEPHn`` 为 alnum，不被 ``_escape`` 改动，由调用方还原。
        """
        return _escape(content)

    def _emit_pandoc_degradation(self) -> None:
        """pandoc 不可用（fallback 策略）时发一条 DEGRADATION 事件（sink 异常不影响导出）。"""
        try:
            self._sink.emit(
                Event(
                    kind=EventKind.DEGRADATION,
                    message="pandoc 不可用，已回退手写渲染器",
                    data={
                        "feature": "pandoc",
                        "reason": "unavailable",
                        "output_format": self.format.value,
                    },
                )
            )
        except Exception:  # noqa: BLE001 - 可观测性不得中断主流程
            pass

    def _emit_figure_degradation(self, fig, reason: str) -> None:
        """图资产不可用时发一条 DEGRADATION 事件（sink 异常不影响导出）。"""
        try:
            self._sink.emit(
                Event(
                    kind=EventKind.DEGRADATION,
                    message="图资产不可用，回退为仅图题/标签",
                    data={
                        "feature": "figure_embed_latex",
                        "reason": reason,
                        "figure_id": getattr(fig, "figure_id", ""),
                    },
                )
            )
        except Exception:  # noqa: BLE001 - 可观测性不得中断主流程
            pass

    def _render_bib(self, ws: PaperWorkspace, keys: dict[str, str]) -> str:
        # 引用闭合：bib 只列被引用文献（即 keys 中的），保持既定顺序。
        entries = []
        for ref in ws.verified_references:
            if ref.id not in keys:
                continue
            key = keys[ref.id]
            authors = " and ".join(ref.authors) if ref.authors else "Unknown"
            year = ref.year if ref.year is not None else ""
            entries.append(
                f"@article{{{key},\n"
                f"  title = {{{ref.title}}},\n"
                f"  author = {{{authors}}},\n"
                f"  year = {{{year}}},\n"
                f"  note = {{{ref.source}:{ref.source_id}}}\n"
                f"}}"
            )
        return "\n\n".join(entries) + ("\n" if entries else "")
