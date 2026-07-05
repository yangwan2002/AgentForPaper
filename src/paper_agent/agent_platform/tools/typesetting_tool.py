"""set_typesetting 工具：为 Word 产出设置正文行距/对齐/首行缩进/字体。

流程：把参数解析为 ``Typesetting`` → 以 docx 格式导出当前论文 → 对产出文件应用
排版规格 → 记录规格到 ``ws.profile['typesetting']``（供续跑与再导出复用）。

排版规格属于**输出偏好**而非论文内容，不经内容护栏；其持久化经仓储原子更新
（不走内容护栏通道），与既有 profile 偏好落地方式一致。
"""

from __future__ import annotations

from paper_agent.agent_platform.models import ALIGNMENT_VALUES, Typesetting
from paper_agent.agent_platform.tools.context import ToolContext
from paper_agent.tools.registry import ToolRegistry
from paper_agent.workspace.models import PaperWorkspace

_TYPESET_SCHEMA = {
    "type": "object",
    "properties": {
        "line_spacing": {
            "type": "number",
            "description": "正文固定行距（磅），如 22 表示固定行距 22 磅。",
        },
        "alignment": {
            "type": "string",
            "enum": list(ALIGNMENT_VALUES),
            "description": "正文对齐方式：left/center/right/justify（两端对齐）。",
        },
        "first_line_indent": {
            "type": "string",
            "description": "首行缩进，如 \"2ch\"（2 字符）、\"24pt\"、\"1cm\"。",
        },
        "font": {"type": "string", "description": "正文字体名，如 宋体 / Times New Roman。"},
        "columns": {
            "type": "integer",
            "minimum": 1,
            "description": "分栏数：2=双栏（小论文常用），1=单栏。节级排版，作用于整篇。",
        },
    },
    "required": [],
}

_TYPESET_DESCRIPTION = (
    "设置 Word 正文段落的排版规格（行距、对齐方式、首行缩进、字体、分栏数）。这是一个"
    "**设置**：只记录规格、不产生文件。之后调用 export_paper（format=docx）导出、或用 "
    "polish_docx_inplace 就地处理原 docx 时，系统会自动把这些规格套用（含双栏等分栏；"
    "标题/参考文献等结构段落不受影响）。用户要「把 docx 设成双栏」等纯排版调整时用本工具"
    "（配合 polish_docx_inplace 作用于原稿），无需跨格式转换。"
)


def _persist_typesetting_mutation(spec: Typesetting):
    def _mutate(ws: PaperWorkspace) -> None:
        ws.profile["typesetting"] = spec.to_dict()

    return _mutate


def _handle_set_typesetting(
    ctx: ToolContext,
    line_spacing: float | None = None,
    alignment: str | None = None,
    first_line_indent: str | None = None,
    font: str | None = None,
    columns: int | None = None,
) -> str:
    spec = Typesetting(
        line_spacing=line_spacing,
        alignment=alignment,
        first_line_indent=first_line_indent,
        font=font,
        columns=columns,
    )
    if spec.is_empty():
        return "未提供任何排版规格，未做变更。请至少指定行距/对齐/首行缩进/字体/分栏之一。"

    # 纯设置：只把排版规格记录到工作区（不导出、不产文件）。导出 docx 时由
    # export_paper 自动套用——这样只有一个地方写 docx，顺序无关、不会互相覆盖。
    ctx.repo.update(ctx.workspace, _persist_typesetting_mutation(spec))
    ctx.session.record("set_typesetting", spec=spec.to_dict())

    applied = _describe_spec(spec)
    return (
        f"已记录排版规格（{applied}），仅对 docx 正文段落生效。"
        f"导出 docx 时会自动应用；请调用 export_paper 导出（格式选 docx）以生成带排版的文件。"
    )


def _describe_spec(spec: Typesetting) -> str:
    bits = []
    if spec.line_spacing is not None:
        bits.append(f"行距{spec.line_spacing}磅")
    if spec.alignment:
        bits.append(f"对齐={spec.alignment}")
    if spec.first_line_indent:
        bits.append(f"首行缩进={spec.first_line_indent}")
    if spec.font:
        bits.append(f"字体={spec.font}")
    if spec.columns is not None:
        bits.append("双栏" if spec.columns == 2 else f"{spec.columns}栏")
    return "、".join(bits)


def register_set_typesetting(registry: ToolRegistry, ctx: ToolContext) -> None:
    registry.register(
        name="set_typesetting",
        description=_TYPESET_DESCRIPTION,
        handler=lambda line_spacing=None, alignment=None, first_line_indent=None, font=None, columns=None: (  # noqa: E501
            _handle_set_typesetting(
                ctx, line_spacing, alignment, first_line_indent, font, columns
            )
        ),
        parameters=_TYPESET_SCHEMA,
    )


__all__ = ["register_set_typesetting"]
