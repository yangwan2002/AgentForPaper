"""绘图后端抽象与 matplotlib 默认实现（venue-templates-figures-tables）。

本模块定义 ``Figure_Renderer`` 所依赖的绘图后端抽象 ``PlottingBackend``
（依赖倒置），以及一个基于 matplotlib 的默认实现 ``MatplotlibBackend``。

matplotlib 为**可选依赖**：模块顶层**绝不** import matplotlib，导入本模块
在 matplotlib 缺失时也不会报错。``MatplotlibBackend.available`` 反映能否成功
导入 matplotlib；``bar_chart`` 内部惰性 import，并在 import pyplot 之前设置
非交互后端 ``Agg``，以避免在无显示环境（如服务器/CI）下触发 GUI 后端问题。

参考 design.md 的 Figure_Renderer / PlottingBackend 小节。
"""

from __future__ import annotations

import importlib.util
from typing import Protocol, runtime_checkable


@runtime_checkable
class PlottingBackend(Protocol):
    """绘图后端抽象协议（依赖倒置）。

    默认实现基于 matplotlib（可选依赖）。任何满足本协议的对象都可注入
    ``Figure_Renderer`` 作为绘图后端。

    Attributes:
        available: 后端当前是否可用（绘图依赖是否就绪）。为 ``False`` 时
            ``Figure_Renderer`` 应降级为既有文字图题行为。
    """

    available: bool

    def bar_chart(
        self,
        title: str,
        labels: list[str],
        values: list[float],
        out_path: str,
    ) -> None:
        """将一组 (label, value) 渲染为柱状图并保存到 ``out_path``。

        Args:
            title: 图标题。
            labels: 每根柱子的类别标签。
            values: 每根柱子的数值，与 ``labels`` 一一对应。
            out_path: 图像文件的输出路径（如 PNG）。
        """
        ...


class MatplotlibBackend:
    """基于 matplotlib 的默认绘图后端（matplotlib 为可选依赖）。

    构造与属性访问都不会 import matplotlib 本身：``available`` 仅通过
    ``importlib.util.find_spec`` 探测 matplotlib 是否可导入，因此在 matplotlib
    缺失时 ``available == False`` 且不抛异常。真正的 import 推迟到 ``bar_chart``
    调用时（惰性导入），并在 import ``pyplot`` 之前设置非交互后端 ``Agg``。
    """

    @property
    def available(self) -> bool:
        """matplotlib 能否被成功导入。

        使用 ``importlib.util.find_spec`` 探测，不真正 import matplotlib，
        因此不产生副作用、缺失时也不报错。探测过程中的任何异常都视为不可用。
        """
        try:
            return importlib.util.find_spec("matplotlib") is not None
        except (ImportError, ValueError, ModuleNotFoundError):
            return False

    def bar_chart(
        self,
        title: str,
        labels: list[str],
        values: list[float],
        out_path: str,
    ) -> None:
        """使用 matplotlib 渲染柱状图并以非交互后端 ``Agg`` 保存到 ``out_path``。

        在 import ``matplotlib.pyplot`` 之前调用 ``matplotlib.use("Agg")``，
        以避免在无显示环境下触发 GUI 后端问题。

        Args:
            title: 图标题。
            labels: 每根柱子的类别标签。
            values: 每根柱子的数值，与 ``labels`` 一一对应。
            out_path: 图像文件的输出路径（如 PNG）。

        Raises:
            RuntimeError: 当 matplotlib 不可用（无法导入）时抛出，供调用方
                捕获并降级为既有文字图题行为。
        """
        try:
            import matplotlib

            matplotlib.use("Agg")  # 惰性设置非交互后端，须在 import pyplot 前。
            import matplotlib.pyplot as plt
        except ImportError as exc:  # pragma: no cover - 依赖缺失路径
            raise RuntimeError(
                "matplotlib 不可用，无法渲染柱状图（可选依赖缺失）。"
            ) from exc

        fig, ax = plt.subplots()
        try:
            ax.bar(labels, values)
            ax.set_title(title)
            fig.tight_layout()
            fig.savefig(out_path)
        finally:
            plt.close(fig)
