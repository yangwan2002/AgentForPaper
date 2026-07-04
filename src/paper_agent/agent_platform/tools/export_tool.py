"""export_paper 工具：把当前论文导出为指定格式（只读工作区，产出文件）。

复用既有 ``export/factory.get_exporter``。可选 ``format`` 参数覆盖工作区默认输出
格式；未给时用 ``ws.output_format``。此工具不修改工作区内容，仅产出文件。

格式闸：本工具保持精简（仅导出）；带确定性格式闸的导出由复合工具
``run_full_pipeline`` 覆盖，二者互补。
"""

from __future__ import annotations

from paper_agent.agent_platform.tools.context import ToolContext
from paper_agent.export.factory import get_exporter
from paper_agent.tools.registry import ToolRegistry
from paper_agent.workspace.models import OutputFormat

_EXPORT_SCHEMA = {
    "type": "object",
    "properties": {
        "format": {
            "type": "string",
            "enum": [f.value for f in OutputFormat],
            "description": "目标输出格式；省略则用论文当前的输出格式。",
        }
    },
    "required": [],
}

_EXPORT_DESCRIPTION = (
    "把当前论文导出为文件（markdown/latex/docx）。返回产出文件路径列表。"
    "此工具只读论文内容、不修改论文。"
)


def _resolve_format(ws, fmt: str | None) -> OutputFormat:
    """把可选的 format 字符串解析为 OutputFormat；非法/缺省回落工作区默认。"""
    if not fmt:
        return ws.output_format
    try:
        return OutputFormat(fmt)
    except ValueError:
        return ws.output_format


def export_paper_files(ws, out_dir: str, out_fmt: OutputFormat) -> tuple[list[str], str]:
    """导出论文并（对 docx）自动套用已保存排版规格，返回 (文件列表, 排版说明后缀)。

    单一导出实现，供 ``export_paper`` 工具与验收收尾（finalize）复用，避免"再导出
    绕过排版应用"的重复与漂移。
    """
    exporter = get_exporter(out_fmt)
    result = exporter.export(ws, out_dir)
    applied_note = _apply_saved_typesetting(ws, out_fmt, result.files)
    return list(result.files), applied_note


def _handle_export(ctx: ToolContext, format: str | None = None) -> str:
    ws = ctx.workspace
    out_fmt = _resolve_format(ws, format)

    # 关键：docx 导出后自动套用已保存的排版规格（ws.profile['typesetting']），
    # 使排版不因「先 set_typesetting 再 export_paper 覆盖」而丢失——无论工具调用顺序，
    # 导出的 docx 始终带上用户设定的行距/对齐/缩进。
    files, applied_note = export_paper_files(ws, ctx.output_dir, out_fmt)

    ctx.session.record("export_paper", format=out_fmt.value, files=list(files))
    if not files:
        return f"导出（{out_fmt.value}）完成，但未产出文件。"
    joined = "、".join(files)
    return f"已导出（{out_fmt.value}）：{joined}{applied_note}"


def _apply_saved_typesetting(ws, out_fmt: OutputFormat, files: list[str]) -> str:
    """docx 导出后，若工作区存有排版规格则应用之，返回说明后缀（无则空串）。"""
    if out_fmt is not OutputFormat.DOCX or not files:
        return ""
    spec_data = ws.profile.get("typesetting") if ws.profile else None
    if not spec_data:
        return ""
    from paper_agent.agent_platform.models import Typesetting
    from paper_agent.export.typesetting import apply_typesetting

    spec = Typesetting.from_dict(spec_data)
    if spec.is_empty():
        return ""
    try:
        apply_typesetting(files[0], spec)
    except Exception as exc:  # noqa: BLE001 - 排版应用失败不影响导出本身
        return f"（注：已导出，但排版应用失败：{exc}）"
    return "（已套用已保存的排版规格）"


def register_export_paper(registry: ToolRegistry, ctx: ToolContext) -> None:
    """把 export_paper 工具注册进 registry。"""
    registry.register(
        name="export_paper",
        description=_EXPORT_DESCRIPTION,
        handler=lambda format=None: _handle_export(ctx, format),
        parameters=_EXPORT_SCHEMA,
    )


__all__ = ["register_export_paper", "export_paper_files"]
