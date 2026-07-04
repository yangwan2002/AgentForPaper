"""结果表渲染器（Table_Renderer，Req 6.1-6.8 / 8.1-8.3）。

把 ``ResearchArtifact.experiments[].results_data`` 渲染为目标格式的结果表：

- ``render_latex`` → 产出 ``table``/``tabular`` 环境（含表头行、``\\caption``、``\\label``）。
- ``render_docx`` → 向传入的 python-docx ``Document`` 追加原生表格（表头行 + 数据行）。

设计要点（对齐 design.md 的 Table_Renderer 小节与需求）：

- 行 = ``baselines``/方法，列 = ``metrics``；每个单元格取对应的 ``results_data`` 数值，
  优先 ``stats[metric].mean``，缺失时回落到 ``rows`` 中与该 baseline 对应的值。
- 每个数值经 ``GroundingChecker.is_grounded`` 校验（不放宽既有质量闸判定）；未通过
  → 跳过该单元格、记入 ``TableFragment.skipped_cells`` 并发 ``DEGRADATION`` 事件
  （``reason=rejected_ungrounded_value``）。
- 单条异常数据（缺字段/非数值）→ 只跳过该单元格并发 ``DEGRADATION``
  （``reason=cell_skipped``），不中止整表。
- 无 artifact / 全部 ``stats`` 为空 → ``render_latex`` 返回 ``[]``、``render_docx`` 返回
  ``0``，并发一条 ``DEGRADATION``（``feature="table"``、``reason="no_data"``、
  ``message`` 含「无可用实验数据，跳过表格生成」），不抛异常。
- 浮点统一 ``float_decimals`` 位格式化；派生文本（列名/行名/图题）截断至
  ``max_field_chars``（默认 500）；LaTeX 特殊字符对派生文本转义，**数值不转义**。
- 视 ``results_data`` 为不可信数据：不 ``eval``/``exec``。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from paper_agent.observability.events import Event, EventKind

# 跳过单元格时写入产物的占位符（既不参与 grounding，也非可解析数值）。
_SKIPPED_PLACEHOLDER = "--"

# LaTeX 特殊字符转义表（与 export/latex.py 同源思路；此处独立定义以避免循环导入）。
_LATEX_SPECIAL = {
    "&": r"\&", "%": r"\%", "$": r"\$", "#": r"\#",
    "_": r"\_", "{": r"\{", "}": r"\}", "~": r"\textasciitilde{}",
    "^": r"\textasciicircum{}",
}


def _escape_latex(text: str) -> str:
    """转义 LaTeX 特殊字符（仅用于派生文本，绝不用于数值）。"""
    return "".join(_LATEX_SPECIAL.get(ch, ch) for ch in text)


@dataclass
class TableFragment:
    """一张结果表的渲染产物（LaTeX）。

    Attributes:
        experiment_id: 该表对应的实验 id。
        caption: 图题文本（已截断，未转义的原文）。
        label: LaTeX ``\\label`` 名（如 ``tab:main``）。
        latex: 完整的 ``table``/``tabular`` 环境字符串。
        skipped_cells: 被跳过的单元格标识列表（``"<baseline>/<metric>"``）。
    """

    experiment_id: str
    caption: str
    label: str
    latex: str
    skipped_cells: list[str] = field(default_factory=list)


class TableRenderer:
    """从 ``ResearchArtifact`` 实验数据渲染结果表（grounding 不放宽）。"""

    def __init__(self, grounding, sink, float_decimals: int = 3,
                 max_field_chars: int = 500) -> None:
        self._grounding = grounding
        self._sink = sink
        self._float_decimals = max(0, int(float_decimals))
        self._max_field_chars = max(1, int(max_field_chars))

    # ------------------------------------------------------------------ #
    # 公共 API
    # ------------------------------------------------------------------ #

    def render_latex(self, artifact) -> list[TableFragment]:
        """为每个含非空 ``stats`` 的实验产出一个 ``TableFragment``。

        无 artifact / 全空 stats → 返回 ``[]`` 并发 ``no_data`` 降级事件。
        """
        experiments = self._experiments_with_stats(artifact)
        if not experiments:
            self._emit_no_data()
            return []

        fragments: list[TableFragment] = []
        for exp in experiments:
            header, rows, skipped = self._build_matrix(exp)
            latex = self._render_fragment_latex(exp, header, rows)
            fragments.append(
                TableFragment(
                    experiment_id=exp.experiment_id,
                    caption=self._caption_for(exp),
                    label=self._label_for(exp),
                    latex=latex,
                    skipped_cells=skipped,
                )
            )
        return fragments

    def render_docx(self, artifact, document) -> int:
        """向 python-docx ``Document`` 追加原生表格，返回追加的表数。

        无 artifact / 全空 stats → 返回 ``0`` 并发 ``no_data`` 降级事件。
        ``document`` 由调用方传入，此处直接使用其 ``add_table``（python-docx 惰性
        由调用方负责导入）。
        """
        experiments = self._experiments_with_stats(artifact)
        if not experiments:
            self._emit_no_data()
            return 0

        appended = 0
        for exp in experiments:
            header, rows, _skipped = self._build_matrix(exp)
            n_cols = len(header)
            n_rows = 1 + len(rows)
            table = document.add_table(rows=n_rows, cols=n_cols)
            # 表头行
            for c, text in enumerate(header):
                table.rows[0].cells[c].text = text
            # 数据行
            for r, row in enumerate(rows, start=1):
                for c, text in enumerate(row):
                    table.rows[r].cells[c].text = text
            appended += 1
        return appended

    # ------------------------------------------------------------------ #
    # 内部：数据抽取与矩阵构造
    # ------------------------------------------------------------------ #

    @staticmethod
    def _experiments_with_stats(artifact) -> list:
        """返回含非空 ``results_data.stats`` 的实验列表。artifact 为 None → []。"""
        if artifact is None:
            return []
        experiments = getattr(artifact, "experiments", None) or []
        out = []
        for exp in experiments:
            results_data = getattr(exp, "results_data", None) or {}
            stats = results_data.get("stats") if isinstance(results_data, dict) else None
            if isinstance(stats, dict) and stats:
                out.append(exp)
        return out

    def _build_matrix(self, exp):
        """构造 (header, rows, skipped_cells)。

        header: ``["Method", metric1, metric2, ...]``（派生文本截断+转义留待渲染层）。
        这里 header 与 rows 里的文本均为「已截断的展示文本」，数值已格式化为字符串。
        skipped_cells: ``"<baseline>/<metric>"`` 列表。
        """
        results_data = getattr(exp, "results_data", None) or {}
        stats = results_data.get("stats") if isinstance(results_data, dict) else {}
        stats = stats if isinstance(stats, dict) else {}
        data_rows = results_data.get("rows") if isinstance(results_data, dict) else []
        data_rows = data_rows if isinstance(data_rows, list) else []

        metrics = list(getattr(exp, "metrics", None) or [])
        if not metrics:
            metrics = list(stats.keys())
        baselines = list(getattr(exp, "baselines", None) or [])
        if not baselines:
            # 无 baselines 时用 dataset / experiment_id 作为单行标签。
            label = getattr(exp, "dataset", "") or getattr(exp, "experiment_id", "")
            baselines = [label or "result"]

        header = ["Method"] + [self._truncate(str(m)) for m in metrics]

        rows: list[list[str]] = []
        skipped: list[str] = []
        for baseline in baselines:
            row_cells = [self._truncate(str(baseline))]
            for metric in metrics:
                cell_id = f"{baseline}/{metric}"
                text = self._resolve_cell(
                    exp, baseline, metric, stats, data_rows, skipped, cell_id
                )
                row_cells.append(text)
            rows.append(row_cells)
        return header, rows, skipped

    def _resolve_cell(self, exp, baseline, metric, stats, data_rows,
                      skipped: list[str], cell_id: str) -> str:
        """解析单个单元格数值文本，处理异常/未 grounded 情况。"""
        raw = self._lookup_value(baseline, metric, stats, data_rows)

        # 缺字段/非数值 → 跳过并记 cell_skipped。
        try:
            num = float(raw)
        except (TypeError, ValueError):
            skipped.append(cell_id)
            self._emit_degradation(
                reason="cell_skipped",
                message="跳过异常单元格（缺字段或非数值）",
                exp=exp,
                cell=cell_id,
            )
            return _SKIPPED_PLACEHOLDER

        # 未通过 grounding → 跳过并记 rejected_ungrounded_value。
        if not self._grounding.is_grounded(num):
            skipped.append(cell_id)
            self._emit_degradation(
                reason="rejected_ungrounded_value",
                message="拒绝未 grounded 的数值",
                exp=exp,
                cell=cell_id,
            )
            return _SKIPPED_PLACEHOLDER

        return self._format_float(num)

    @staticmethod
    def _lookup_value(baseline, metric, stats: dict, data_rows: list):
        """取单元格数值：优先该 baseline 自己在 ``rows`` 中的值，缺失才回落聚合量。

        修复：对比表每行是一个方法/基线，单元格必须是**该方法自己**的指标值，
        而非跨行聚合的 ``mean``——否则所有方法显示同一个数字，表格失去意义。

        1) 在 ``rows`` 中找出「任一字段值等于 baseline 名」的行，取其 ``metric`` 字段。
        2) 找不到对应行时（如仅有聚合统计、无逐行数据），回落 ``stats[metric].mean``。
        3) 再找不到返回 ``None``（由调用方按缺字段处理）。
        """
        for row in data_rows:
            if not isinstance(row, dict):
                continue
            if any(str(v) == str(baseline) for v in row.values()):
                if metric in row:
                    return row[metric]

        stat = stats.get(metric)
        if isinstance(stat, dict) and "mean" in stat:
            return stat["mean"]
        return None

    # ------------------------------------------------------------------ #
    # 内部：LaTeX 渲染
    # ------------------------------------------------------------------ #

    def _render_fragment_latex(self, exp, header: list[str], rows: list[list[str]]) -> str:
        n_cols = len(header)
        col_spec = "l" + " c" * (n_cols - 1) if n_cols > 1 else "l"
        caption = _escape_latex(self._caption_for(exp))
        label = self._label_for(exp)

        header_line = " & ".join(_escape_latex(h) for h in header) + r" \\"
        body_lines = []
        for row in rows:
            # 第一列为派生文本需转义；数值列已是格式化字符串（含占位符），
            # 占位符 "--" 无特殊字符，格式化数值不含特殊字符，故转义安全无副作用。
            cells = [_escape_latex(row[0])] + [_escape_latex(c) for c in row[1:]]
            body_lines.append(" & ".join(cells) + r" \\")

        parts = [
            r"\begin{table}[h]",
            r"\centering",
            rf"\caption{{{caption}}}",
            rf"\label{{{label}}}",
            rf"\begin{{tabular}}{{{col_spec}}}",
            r"\hline",
            header_line,
            r"\hline",
            *body_lines,
            r"\hline",
            r"\end{tabular}",
            r"\end{table}",
        ]
        return "\n".join(parts)

    # ------------------------------------------------------------------ #
    # 内部：文本/数值处理与事件
    # ------------------------------------------------------------------ #

    def _caption_for(self, exp) -> str:
        dataset = getattr(exp, "dataset", "") or ""
        exp_id = getattr(exp, "experiment_id", "") or "experiment"
        base = f"Results on {dataset}" if dataset else f"Results for {exp_id}"
        return self._truncate(base)

    @staticmethod
    def _label_for(exp) -> str:
        exp_id = getattr(exp, "experiment_id", "") or "experiment"
        return f"tab:{exp_id}"

    def _format_float(self, num: float) -> str:
        return f"{num:.{self._float_decimals}f}"

    def _truncate(self, text: str) -> str:
        if len(text) > self._max_field_chars:
            return text[: self._max_field_chars]
        return text

    def _emit_no_data(self) -> None:
        self._emit(
            Event(
                kind=EventKind.DEGRADATION,
                message="无可用实验数据，跳过表格生成",
                data={"feature": "table", "reason": "no_data"},
            )
        )

    def _emit_degradation(self, reason: str, message: str, exp, cell: str) -> None:
        self._emit(
            Event(
                kind=EventKind.DEGRADATION,
                message=message,
                data={
                    "feature": "table",
                    "reason": reason,
                    "experiment_id": getattr(exp, "experiment_id", ""),
                    "cell": cell,
                },
            )
        )

    def _emit(self, event: Event) -> None:
        if self._sink is None:
            return
        self._sink.emit(event)


__all__ = ["TableFragment", "TableRenderer"]
