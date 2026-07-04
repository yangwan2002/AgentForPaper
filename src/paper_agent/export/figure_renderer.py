"""数据出图渲染器（venue-templates-figures-tables，Req 7 / 8.3）。

``FigureRenderer`` 从 :class:`~paper_agent.workspace.research_artifact.ResearchArtifact`
的实验数据出图（柱状图），产出 :class:`RenderedFigure`（含 :class:`FigureRecord`
与落盘图像文件的绝对路径）。渲染器是**纯数据产出组件**：只负责画图、落盘图像、
产出记录，**绝不写工作区**——写回由 ``WritingAgent`` 经 ``AgentResult.mutations``
在任务 15.1 完成（单一写入路径，Req 7.3 / 9.1）。

关键约定（参考 design.md 的 Figure_Renderer 小节）：

- **grounding 不放宽**：只把 ``grounding.is_grounded`` 通过的数据点写入图；被拒
  数值不入图并发一条 ``DEGRADATION``（reason=``rejected_ungrounded_value``）。所画
  数值取自 ``results_data``（原始行值或 ``stats[metric].mean``），二者都落在既有
  质量闸的允许集合内，从而绝不触发 ``fabricated_metric``（Req 7.2 / 8.1 / 8.2）。
- **优雅降级**（Req 7.5 / 7.6 / 10.6）：
  - ``enabled=False`` → 返回 ``[]``，不发降级事件（正常关闭）。
  - ``backend.available=False`` → 返回 ``[]``，发一条 ``DEGRADATION``
    （feature=``figure_render``, reason=``missing_dependency``）。
  - 无 artifact / 无数据 → 返回 ``[]``。
  - ``backend.bar_chart`` 抛异常（失败/超时）→ 捕获、跳过该图、发 ``DEGRADATION``，
    绝不抛出使管线中止的异常。
- **可观测与记账**（Req 7.7 / 10.4）：外部绘图调用经 ``tracker``（可能为 ``None``）
  记账、经 ``sink`` 记 ``EXPORT_ASSET`` 事件；写入事件的文本片段截断至 2000 字符。
- **不可信数据**：派生文本（标签/图题）截断至 500 字符，不执行 ``eval``/``exec``。
"""

from __future__ import annotations

import os
from dataclasses import dataclass

from paper_agent.observability.events import Event, EventKind, EventSink
from paper_agent.observability.usage import UsageTracker
from paper_agent.workspace.models import FigureRecord
from paper_agent.workspace.research_artifact import ResearchArtifact

from .grounding import GroundingChecker
from .plotting import PlottingBackend

# 派生文本（标签、图题）防御式截断上限（Req 8.3 / 6.7 口径一致）。
_MAX_FIELD_CHARS = 500
# 可观测文本片段截断上限（Req 10.4）。
_MAX_EVENT_CHARS = 2000


def _truncate(text: str, limit: int) -> str:
    """把 ``text`` 规整为字符串并截断至 ``limit`` 字符（防御式）。"""
    s = "" if text is None else str(text)
    return s[:limit]


@dataclass
class RenderedFigure:
    """一次数据出图的产物（渲染中间物，不持久化）。

    Attributes:
        record: 对应的 :class:`FigureRecord`（``rendered_from_data=True``）。
        asset_path: 落盘图像文件的**绝对路径**。
        source_experiment_id: 本图来源实验 id。
    """

    record: FigureRecord
    asset_path: str
    source_experiment_id: str


class FigureRenderer:
    """从实验数据出图的渲染器（依赖注入绘图后端，可选依赖优雅降级）。

    Args:
        backend: 绘图后端（:class:`PlottingBackend`）；``available=False`` 时降级。
        grounding: grounding 校验器；只有通过校验的数值才入图。
        sink: 事件接收器，用于发降级/资产事件。
        tracker: 用量统计器（可能为 ``None``）；记录外部绘图调用。
        enabled: 数据出图开关；``False`` 时不产图、不发降级事件（正常关闭）。
    """

    def __init__(
        self,
        backend: PlottingBackend,
        grounding: GroundingChecker,
        sink: EventSink,
        tracker: UsageTracker | None,
        enabled: bool = True,
    ) -> None:
        self.backend = backend
        self.grounding = grounding
        self.sink = sink
        self.tracker = tracker
        self.enabled = enabled

    # --- 公共 API ---

    def render_from_artifact(
        self, artifact: ResearchArtifact | None, assets_dir: str
    ) -> list[RenderedFigure]:
        """从 ``artifact`` 的实验数据出图，返回 :class:`RenderedFigure` 列表。

        禁用 / 后端不可用 / 无数据 → 返回 ``[]``（后两者分别发/不发降级事件）。
        单张图渲染失败只跳过该图，绝不中止整体（Req 10.6）。
        """
        if not self.enabled:
            # 配置禁用：正常关闭，不发降级事件（Req 7.6）。
            return []

        if not getattr(self.backend, "available", False):
            # 绘图依赖不可用：降级为文字图题（由调用方 WritingAgent 兜底）。
            self._emit_degradation(
                reason="missing_dependency",
                message="绘图依赖不可用，已降级为文字图题",
            )
            return []

        experiments = getattr(artifact, "experiments", None) or []
        if artifact is None or not experiments:
            return []

        rendered: list[RenderedFigure] = []
        for exp in experiments:
            results_data = getattr(exp, "results_data", None) or {}
            if not results_data:
                continue
            rendered.extend(self._render_experiment(exp, results_data, assets_dir))
        return rendered

    # --- 内部实现 ---

    def _render_experiment(
        self, exp, results_data: dict, assets_dir: str
    ) -> list[RenderedFigure]:
        """为单个实验的每个指标出一张柱状图。"""
        rows = results_data.get("rows") or []
        stats = results_data.get("stats") or {}

        # 指标集合：优先 experiment.metrics，缺失时回落到 stats 的键。
        metrics = list(getattr(exp, "metrics", None) or [])
        if not metrics:
            metrics = [k for k in stats.keys()]

        out: list[RenderedFigure] = []
        for metric in metrics:
            labels, values = self._collect_series(exp, metric, rows, stats)
            if not values:
                # 该指标无可用（grounded）数据点：跳过，不产空图。
                continue
            figure = self._render_one(exp, metric, labels, values, assets_dir)
            if figure is not None:
                out.append(figure)
        return out

    def _collect_series(
        self, exp, metric: str, rows: list, stats: dict
    ) -> tuple[list[str], list[float]]:
        """收集某指标的 (labels, values) 序列，仅保留 grounded 数值。

        优先从 ``rows`` 逐行取原始值（每根柱=一个 baseline/方法）；行不可用时
        回落到 ``stats[metric].mean`` 作为单根柱。非数值单元/非 grounded 数值被
        跳过并记 ``DEGRADATION``（不中止整图）。
        """
        labels: list[str] = []
        values: list[float] = []

        baselines = list(getattr(exp, "baselines", None) or [])
        label_key = self._infer_label_key(rows, metric)

        for i, row in enumerate(rows):
            if not isinstance(row, dict) or metric not in row:
                continue
            try:
                val = float(row.get(metric))
            except (TypeError, ValueError):
                # 单条异常数据：跳过该单元格并记录，不中止整图（Req 8.3）。
                self._emit_degradation(
                    reason="cell_skipped",
                    message=f"实验 {getattr(exp, 'experiment_id', '')} 指标 "
                    f"{metric} 存在非数值单元，已跳过",
                )
                continue
            if not self.grounding.is_grounded(val):
                self._emit_degradation(
                    reason="rejected_ungrounded_value",
                    message=f"数值 {val} 未通过 grounding 校验，未写入图 "
                    f"({getattr(exp, 'experiment_id', '')}/{metric})",
                )
                continue
            if label_key is not None:
                label = row.get(label_key, "")
            elif i < len(baselines):
                label = baselines[i]
            else:
                label = f"{metric} #{i}"
            labels.append(_truncate(label, _MAX_FIELD_CHARS))
            values.append(val)

        if values:
            return labels, values

        # 回落：stats[metric].mean 作为单根柱（仍需 grounded）。
        metric_stats = stats.get(metric)
        if isinstance(metric_stats, dict) and metric_stats.get("mean") is not None:
            try:
                mean_val = float(metric_stats["mean"])
            except (TypeError, ValueError):
                return [], []
            if self.grounding.is_grounded(mean_val):
                return [_truncate(metric, _MAX_FIELD_CHARS)], [mean_val]
            self._emit_degradation(
                reason="rejected_ungrounded_value",
                message=f"stats.mean {mean_val} 未通过 grounding 校验，未写入图 "
                f"({getattr(exp, 'experiment_id', '')}/{metric})",
            )
        return [], []

    @staticmethod
    def _infer_label_key(rows: list, metric: str) -> str | None:
        """推断行数据中的「标签列」（承载 baseline/方法名的非数值列）。

        取首行的键，选第一个「其值在各行都不可解析为 float」的键作为标签列；
        找不到则返回 ``None``（由调用方回落到 baselines/序号）。
        """
        if not rows or not isinstance(rows[0], dict):
            return None
        candidate_keys = [k for k in rows[0].keys() if k != metric]
        for key in candidate_keys:
            non_numeric = True
            for row in rows:
                if not isinstance(row, dict) or key not in row:
                    continue
                try:
                    float(row.get(key))
                    non_numeric = False
                    break
                except (TypeError, ValueError):
                    continue
            if non_numeric:
                return key
        return None

    def _render_one(
        self, exp, metric: str, labels: list[str], values: list[float], assets_dir: str
    ) -> RenderedFigure | None:
        """渲染并落盘单张柱状图，产出 :class:`RenderedFigure`。

        绘图后端抛异常（失败/超时）→ 捕获、发降级、返回 ``None``（跳过该图，
        不中止管线，Req 10.6）。
        """
        experiment_id = getattr(exp, "experiment_id", "") or "exp"
        figure_id = f"fig_{experiment_id}_{metric}"
        asset_filename = f"{figure_id}.png"
        asset_path = os.path.join(assets_dir, asset_filename)
        title = _truncate(f"{experiment_id} - {metric}", _MAX_FIELD_CHARS)

        try:
            os.makedirs(assets_dir, exist_ok=True)
            self.backend.bar_chart(title, labels, values, asset_path)
        except Exception as exc:  # noqa: BLE001 - 任何绘图失败都降级，不外抛。
            self._emit_degradation(
                reason="render_failed",
                message=f"绘图失败，已跳过该图 ({figure_id})：{exc}",
            )
            return None

        # 记账：外部绘图调用（tracker 可能为 None）。
        if self.tracker is not None:
            self.tracker.add("", "", prompt_tokens=0, completion_tokens=0)

        # 落盘资产事件（Req 7.7 / EXPORT_ASSET）。
        self.sink.emit(
            Event(
                kind=EventKind.EXPORT_ASSET,
                message=_truncate(f"图像已落盘：{asset_path}", _MAX_EVENT_CHARS),
                data={
                    "asset_type": "figure",
                    "figure_id": figure_id,
                    "path": _truncate(asset_path, _MAX_EVENT_CHARS),
                    "source_experiment_id": experiment_id,
                },
            )
        )

        caption = _truncate(
            f"实验 {experiment_id} 在指标 {metric} 上的结果对比。",
            _MAX_FIELD_CHARS,
        )
        record = FigureRecord(
            figure_id=figure_id,
            # data_ref 为相对 assets_dir 的图像相对路径（资产直接落在 assets_dir 下）。
            data_ref=asset_filename,
            caption=caption,
            caption_provided_by_user=False,
            source_experiment_id=experiment_id,
            rendered_from_data=True,
        )
        return RenderedFigure(
            record=record,
            asset_path=asset_path,
            source_experiment_id=experiment_id,
        )

    def _emit_degradation(self, reason: str, message: str) -> None:
        """发一条 ``DEGRADATION`` 事件（feature 固定为 ``figure_render``）。"""
        self.sink.emit(
            Event(
                kind=EventKind.DEGRADATION,
                message=_truncate(message, _MAX_EVENT_CHARS),
                data={"feature": "figure_render", "reason": reason},
            )
        )


__all__ = ["RenderedFigure", "FigureRenderer"]
