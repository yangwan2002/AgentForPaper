"""导出器工厂：按输出格式返回对应导出器（Req 10.4）。

LaTeX / Markdown 零额外依赖直接可用；docx 依赖可选包，
未安装时在调用 export 时给出明确错误。未注册的格式回退到 Markdown。
"""

from __future__ import annotations

from paper_agent.export.base import DocumentExporter
from paper_agent.export.docx import DocxExporter
from paper_agent.export.latex import LatexExporter
from paper_agent.export.markdown import MarkdownExporter
from paper_agent.workspace.models import OutputFormat

_REGISTRY: dict[OutputFormat, type] = {
    OutputFormat.MARKDOWN: MarkdownExporter,
    OutputFormat.LATEX: LatexExporter,
    OutputFormat.DOCX: DocxExporter,
}


def register_exporter(fmt: OutputFormat, exporter_cls: type) -> None:
    _REGISTRY[fmt] = exporter_cls


def get_exporter(fmt: OutputFormat) -> DocumentExporter:
    exporter_cls = _REGISTRY.get(fmt, MarkdownExporter)
    return exporter_cls()
