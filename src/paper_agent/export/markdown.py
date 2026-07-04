"""Markdown 导出器（Phase 1 默认）。

零依赖直接渲染，便于预览与调试（Req 7.1）。本导出器**从不**调用任何外部
可执行程序（pandoc/pdflatex/subprocess）——它连 `subprocess` 都不导入，因此
即使 PATH 中缺失这些工具仍能成功产出 `<id>.md`，且**不标注任何降级**（Req 7.2）。

内容契约保真（Req 7.4/7.6）：章节正文按**字节原样**渲染，保留章节顺序与标题
层级；数学（`$...$` / `$$...$$`）、代码与方括号文献引用标注（`[id]`）一律原样
保留，**不做任何转义或改写**（与 Req 6.3 数学语义不被破坏对齐）。故此处不对正文
做 `[id]→[n]` 的行内改写——那会改变正文字节，违反 Req 7.4/7.6；LaTeX/docx 导出
器仍各自按其格式约定处理引用。

图表说明与参考文献（Req 7.5）：编号取值与排列顺序与 `verified_references` /
`figures` 的既定顺序保持一致。空章节集合时产出仅含结构骨架的 `<id>.md`（Req 7.7）。
"""

from __future__ import annotations

import os

from paper_agent.export.atomic_write import atomic_write_text
from paper_agent.export.base import ExportResult
from paper_agent.export.citation_closure import cited_references
from paper_agent.workspace.models import OutputFormat, PaperWorkspace


class MarkdownExporter:
    format = OutputFormat.MARKDOWN

    def export(self, ws: PaperWorkspace, out_dir: str) -> ExportResult:
        # 仅用标准库文件写入 + 字符串拼接；绝不调用外部工具（Req 7.1/7.2）。
        os.makedirs(out_dir, exist_ok=True)
        lines: list[str] = []

        # 引用闭合：参考文献表只列被正文实际引用的文献（编号稳定，保留既定顺序）。
        # 正文字节不因此改变——闭合只作用于参考文献表段（不违反 Req 7.4/7.6 正文契约）。
        refs = cited_references(ws)
        ref_index = {r.id: i + 1 for i, r in enumerate(refs)}

        for node in ws.ordered_sections():
            draft = ws.section_drafts.get(node.section_id)
            # 标题层级：大纲章节统一为 ATX 一级标题（Req 7.4 保留层级/顺序）。
            lines.append(f"# {node.title}\n")
            if draft:
                # 正文字节原样渲染：数学/代码/[id] 不转义、不改写（Req 7.4/7.6）。
                lines.append(draft.content + "\n")

        # 图表说明（Req 7.5：按 figures 既定顺序，编号/排列一致）。
        if ws.figures:
            lines.append("# 图表\n")
            for fig in ws.figures:
                lines.append(f"- {fig.figure_id}: {fig.caption}\n")

        # 参考文献（Req 7.5：编号取值与排列顺序一致；仅列被引用者）。
        if refs:
            lines.append("# 参考文献\n")
            for r in refs:
                authors = ", ".join(r.authors)
                year = r.year if r.year is not None else "n.d."
                lines.append(
                    f"{ref_index[r.id]}. {authors} ({year}). {r.title}. "
                    f"{r.source}:{r.source_id}\n"
                )

        # 空章节集合（且无图表/文献）→ 产出仅含结构骨架的 <id>.md，不报错（Req 7.7）。
        if not lines:
            lines.append(f"# {ws.workspace_id}\n")

        path = os.path.join(out_dir, f"{ws.workspace_id}.md")
        # UTF-8 原子落盘（Req 7.2/7.3）：tmp-then-rename，崩溃不留半截文件。
        atomic_write_text(path, "\n".join(lines))

        return ExportResult(output_format=self.format, files=[path])
