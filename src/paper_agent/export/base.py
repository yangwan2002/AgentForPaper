"""文档导出器接口（Req 10）。

输出格式与写作/评审逻辑解耦：智能体只产出中性内部表示，
导出器据 workspace.output_format 渲染为目标格式文件。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

from paper_agent.workspace.models import OutputFormat, PaperWorkspace


@dataclass
class ExportResult:
    output_format: OutputFormat
    files: list[str] = field(default_factory=list)  # 生成文件的路径
    notes: list[str] = field(default_factory=list)  # 导出过程中的提示/警告信息（默认空，向后兼容）


@runtime_checkable
class DocumentExporter(Protocol):
    format: OutputFormat

    def export(self, ws: PaperWorkspace, out_dir: str) -> ExportResult:
        ...
