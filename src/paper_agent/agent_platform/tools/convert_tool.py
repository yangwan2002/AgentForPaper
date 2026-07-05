"""convert_document 工具：用户原文件的**跨格式直转**（保公式/结构，可选双栏）。

问题背景：把用户的 `.tex` 转 docx 时，走 import_draft（抽纯文本）→ 当 Markdown → 重建
docx 的路径会**丢公式、乱结构**（LaTeX 数学被当普通文字）。正确做法是让 pandoc 以
**LaTeX 为输入格式直转** docx（``pandoc -f latex -t docx``）——公式变 Word 原生公式、
章节结构保留。

本工具就是这条"源文件直转"路径：拿用户原始 `.tex`/`.docx`/`.md` 当真相，用
:meth:`PandocConverter.convert_file` 直转目标格式；docx 目标可选叠加**双栏**（Word
sectPr 分栏）与已保存的排版规格。是"用户给 A 格式、要 B 格式"这类纯转换任务的正确工具，
不走重建。
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field

from paper_agent.agent_platform.models import Typesetting
from paper_agent.agent_platform.tools.context import ToolContext
from paper_agent.export.pandoc_pipeline import PandocConverter
from paper_agent.tools.registry import ToolRegistry


@dataclass
class ConvertOutcome:
    """跨格式转换的结构化结果（供工具与工作流复用）。

    ``notes`` 是可直接拼接成用户可读文本的片段列表（首片无前导空格、后续含前导
    空格，``"".join(notes)`` 即完整说明）；失败时 ``ok=False`` 且 ``error`` 非空。
    """

    ok: bool
    files: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    error: str = ""

    def message(self) -> str:
        """人可读结论：失败给 error，成功给拼接后的 notes。"""
        return self.error if not self.ok else "".join(self.notes)

# 源文件扩展名 → pandoc 输入格式。
_EXT_TO_FROM = {
    ".tex": "latex", ".latex": "latex",
    ".docx": "docx",
    ".md": "markdown", ".markdown": "markdown",
}
# 目标格式 → (pandoc 输出格式, 产物扩展名)。
_TO_FORMAT = {
    "docx": ("docx", ".docx"),
    "latex": ("latex", ".tex"),
    "markdown": ("markdown", ".md"),
}

_SCHEMA = {
    "type": "object",
    "properties": {
        "to_format": {
            "type": "string",
            "enum": sorted(_TO_FORMAT),
            "description": "目标格式：docx / latex / markdown。",
        },
        "path": {
            "type": "string",
            "description": "原文件绝对路径；省略则用本会话已导入的原文件。",
        },
        "two_column": {
            "type": "boolean",
            "description": "仅对 docx 目标有效：是否设为双栏排版（小论文常用）。默认 false。",
        },
        "three_line_table": {
            "type": "boolean",
            "description": (
                "仅对 docx 目标有效：是否把表格套用三线表（booktabs）样式——学术论文"
                "标准表格。默认 true。"
            ),
        },
    },
    "required": ["to_format"],
}

_DESCRIPTION = (
    "把用户的原文件**跨格式直转**为目标格式（如 .tex → docx），用 pandoc 直接转换，"
    "**保留公式与章节结构**（LaTeX 公式会转成 Word 原生公式）。docx 目标可选设双栏、"
    "并套用 set_typesetting 的排版。当用户要求「把 X 格式转成 Y 格式」这类纯转换时用本"
    "工具；不要用 import_draft + export_paper 重建（那会丢公式、乱结构）。"
)


def _resolve_source(ctx: ToolContext, path: str | None) -> tuple[str | None, str | None]:
    """解析源文件路径与 pandoc 输入格式；无法解析返回 (None, None)。"""
    candidate = (path or "").strip().strip('"').strip("'")
    if not candidate:
        profile = getattr(ctx.workspace, "profile", None) or {}
        candidate = profile.get("source_document_path", "")
    if not candidate or not os.path.isfile(candidate):
        return None, None
    ext = os.path.splitext(candidate)[1].lower()
    from_format = _EXT_TO_FROM.get(ext)
    return (candidate, from_format) if from_format else (candidate, None)


def _set_two_columns(docx_path: str) -> None:
    """把 docx 所有 section 设为双栏。委托给分栏排版原语（单一实现，避免重复）。"""
    from paper_agent.export.typesetting import apply_columns

    apply_columns(docx_path, 2)


def _fix_table_widths(docx_path: str) -> None:
    """把 docx 表格的固定列宽清成自适应（autofit），消除窄栏里逐字符压缩换行。

    pandoc 从 LaTeX 转出的表格常带固定 ``tcW`` / ``tblW`` 宽度；放进双栏窄栏时 Word
    硬按固定宽度塞，导致每列被压到最小、内容逐字符换行。改为 autofit（表格与单元格
    宽度设 ``auto`` + ``tblLayout=autofit``）后，Word 按内容重算列宽。
    """
    import docx  # noqa: WPS433
    from docx.oxml import OxmlElement
    from docx.oxml.ns import qn

    document = docx.Document(docx_path)
    for table in document.tables:
        table.allow_autofit = True
        tbl = table._tbl
        tbl_pr = tbl.tblPr
        # tblLayout = autofit（否则 Word 用固定布局，严格按 gridCol 比例压缩）。
        layout = tbl_pr.find(qn("w:tblLayout"))
        if layout is None:
            layout = OxmlElement("w:tblLayout")
            tbl_pr.append(layout)
        layout.set(qn("w:type"), "autofit")
        # 表格总宽 → auto。
        for tbl_w in tbl_pr.findall(qn("w:tblW")):
            tbl_w.set(qn("w:w"), "0")
            tbl_w.set(qn("w:type"), "auto")
        # 各单元格固定宽度 → auto（关键：消除逐字符压缩）。
        for tc_w in tbl.iter(qn("w:tcW")):
            tc_w.set(qn("w:w"), "0")
            tc_w.set(qn("w:type"), "auto")
    document.save(docx_path)


def _apply_three_line_table_style(docx_path: str) -> None:
    """给 docx 所有表格套用**三线表（booktabs）**样式：仅顶线、表头下线、底线；
    去掉所有竖线与其它横线——学术论文标准表格样式。

    边框粗细（sz 单位为 1/8 pt）：顶/底线 1.5pt（sz=12），表头下线 0.75pt（sz=6）。
    """
    import docx  # noqa: WPS433
    from docx.oxml import OxmlElement
    from docx.oxml.ns import qn

    def _border(name: str, *, val: str, sz: int = 0) -> "OxmlElement":
        el = OxmlElement(f"w:{name}")
        el.set(qn("w:val"), val)
        if val != "none":
            el.set(qn("w:sz"), str(sz))
            el.set(qn("w:space"), "0")
            el.set(qn("w:color"), "000000")
        return el

    document = docx.Document(docx_path)
    for table in document.tables:
        tbl_pr = table._tbl.tblPr
        # 重置表格边框：顶/底粗线，左右/内部竖线/内部横线全去掉。
        old = tbl_pr.find(qn("w:tblBorders"))
        if old is not None:
            tbl_pr.remove(old)
        borders = OxmlElement("w:tblBorders")
        borders.append(_border("top", val="single", sz=12))
        borders.append(_border("bottom", val="single", sz=12))
        borders.append(_border("left", val="none"))
        borders.append(_border("right", val="none"))
        borders.append(_border("insideH", val="none"))
        borders.append(_border("insideV", val="none"))
        tbl_pr.append(borders)
        # 表头行（第一行）底部加细线（\midrule）。
        if table.rows:
            for cell in table.rows[0].cells:
                tc_pr = cell._tc.get_or_add_tcPr()
                tc_borders = tc_pr.find(qn("w:tcBorders"))
                if tc_borders is None:
                    tc_borders = OxmlElement("w:tcBorders")
                    tc_pr.append(tc_borders)
                existing_bottom = tc_borders.find(qn("w:bottom"))
                if existing_bottom is not None:
                    tc_borders.remove(existing_bottom)
                tc_borders.append(_border("bottom", val="single", sz=6))
    document.save(docx_path)


def _apply_typesetting_if_any(ctx: ToolContext, docx_path: str) -> str:
    """docx 目标：套用已保存的排版规格（set_typesetting 落定的），返回说明后缀。"""
    profile = getattr(ctx.workspace, "profile", None) or {}
    spec_data = profile.get("typesetting")
    if not spec_data:
        return ""
    spec = Typesetting.from_dict(spec_data)
    if spec.is_empty():
        return ""
    try:
        from paper_agent.export.typesetting import apply_typesetting

        apply_typesetting(docx_path, spec)
        return " 已套用排版规格。"
    except Exception as exc:  # noqa: BLE001 - 排版失败不影响转换产物
        return f"（排版应用失败：{exc}）"


def convert_document_core(
    ctx: ToolContext, to_format: str, path: str | None = None,
    two_column: bool = False, three_line_table: bool = True,
) -> ConvertOutcome:
    """跨格式直转的**确定性核心**：解析源 → pandoc 直转 → docx 后处理（表格/双栏/排版）。

    返回结构化 :class:`ConvertOutcome`，供 ``convert_document`` 工具与 ``ConvertWorkflow``
    共享同一实现（工具只负责格式化文本、工作流只负责按固定序编排 + 诚实上报）。
    产物写新文件、原稿只读；任何一步失败经 ``ok/error`` 诚实上报，不降级重建。
    """
    to_format = (to_format or "").lower()
    if to_format not in _TO_FORMAT:
        return ConvertOutcome(
            ok=False, error=f"不支持的目标格式：{to_format}；支持 {sorted(_TO_FORMAT)}。"
        )

    src, from_format = _resolve_source(ctx, path)
    if src is None:
        return ConvertOutcome(
            ok=False,
            error="未找到可转换的源文件：请提供原文件的绝对路径，或先用 import_draft 导入。",
        )
    if from_format is None:
        return ConvertOutcome(
            ok=False,
            error=f"无法识别源文件类型（{os.path.splitext(src)[1]}），支持 .tex/.docx/.md。",
        )

    pandoc = PandocConverter()
    if not pandoc.probe():
        return ConvertOutcome(
            ok=False,
            error=(
                "转换失败：未检测到 pandoc（跨格式直转依赖它）。若已安装但不在 PATH，"
                "请设置环境变量 PANDOC_PATH 指向 pandoc.exe（如 "
                "PANDOC_PATH=D:\\path\\to\\pandoc.exe）；未安装见 "
                "https://pandoc.org/installing.html。"
            ),
        )

    out_format, out_ext = _TO_FORMAT[to_format]
    if from_format == out_format:
        return ConvertOutcome(
            ok=True, notes=[f"源文件已是 {to_format} 格式，无需转换。"]
        )

    stem = os.path.splitext(os.path.basename(src))[0]
    os.makedirs(ctx.output_dir, exist_ok=True)
    out_path = os.path.join(ctx.output_dir, f"{stem}_converted{out_ext}")

    result = pandoc.convert_file(
        src, out_path, from_format=from_format, to_format=out_format
    )
    if not result.ok:
        return ConvertOutcome(
            ok=False,
            error=f"pandoc 转换失败（exit={result.exit_code}）：{result.stderr[:300]}",
        )

    notes = [f"已直转为 {to_format}：{out_path}（公式与结构由 pandoc 保留）。"]
    if to_format == "docx":
        # 先修表格列宽（清固定宽度为自适应），避免双栏窄栏里逐字符压缩。
        try:
            _fix_table_widths(out_path)
            notes.append(" 表格已设自适应列宽。")
        except Exception as exc:  # noqa: BLE001 - 表格修复失败不影响主产物
            notes.append(f"（表格宽度修复失败：{exc}）")
        if three_line_table:
            try:
                _apply_three_line_table_style(out_path)
                notes.append(" 表格已套用三线表样式。")
            except Exception as exc:  # noqa: BLE001 - 样式失败不影响主产物
                notes.append(f"（三线表样式应用失败：{exc}）")
        if two_column:
            try:
                _set_two_columns(out_path)
                notes.append(" 已设为双栏。")
            except Exception as exc:  # noqa: BLE001 - 双栏失败不影响主产物
                notes.append(f"（双栏设置失败：{exc}）")
        ts_note = _apply_typesetting_if_any(ctx, out_path)
        if ts_note:
            notes.append(ts_note)

    ctx.session.record("convert_document", source=src, to=to_format, files=[out_path])
    return ConvertOutcome(ok=True, files=[out_path], notes=notes)


def _handle_convert(
    ctx: ToolContext, to_format: str, path: str | None = None,
    two_column: bool = False, three_line_table: bool = True,
) -> str:
    """convert_document 工具入口：调确定性核心，返回人可读文本。"""
    outcome = convert_document_core(ctx, to_format, path, two_column, three_line_table)
    return outcome.message()


def register_convert_document(registry: ToolRegistry, ctx: ToolContext) -> None:
    """把 convert_document 工具注册进 registry。"""
    registry.register(
        name="convert_document",
        description=_DESCRIPTION,
        handler=lambda to_format, path=None, two_column=False, three_line_table=True: (
            _handle_convert(ctx, to_format, path, two_column, three_line_table)
        ),
        parameters=_SCHEMA,
    )


__all__ = ["register_convert_document", "convert_document_core", "ConvertOutcome"]
