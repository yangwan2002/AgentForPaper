"""导出期 grounding 适配器：复用确定性质量闸的数值核验逻辑。

本模块**不新增、不放宽**任何判定路径——仅将
``paper_agent.tools.quality_gate`` 已提炼的模块级函数
（``build_allowed_values`` / ``value_matches``）包装成面向导出流程的
``GroundingChecker`` 适配器，避免重复实现允许数值集合的构造与浮点容差比较。

防御式约定：
- ``artifact`` 为 ``None`` 时，``allowed_values()`` 返回 ``[]``，
  ``is_grounded(...)`` 返回 ``False``——不抛异常。
"""

from __future__ import annotations

from paper_agent.tools.quality_gate import build_allowed_values, value_matches


class GroundingChecker:
    """artifact 数值 grounding 检查适配器（复用 quality_gate 判定逻辑）。

    只做适配复用：``allowed_values`` 委托 ``build_allowed_values``，
    ``is_grounded`` 委托 ``value_matches``。allowed values 首次计算后缓存，
    避免重复构造。
    """

    def __init__(self, artifact) -> None:
        self._artifact = artifact
        self._cached_allowed: list[float] | None = None

    def allowed_values(self) -> list[float]:
        """返回允许的数值集合（去重排序）。artifact 为 None 时返回 ``[]``。

        委托 ``build_allowed_values(artifact)``；结果缓存以避免重复构造。
        """
        if self._cached_allowed is None:
            if self._artifact is None:
                self._cached_allowed = []
            else:
                self._cached_allowed = build_allowed_values(self._artifact)
        return self._cached_allowed

    def is_grounded(self, value: float, tolerance: float = 0.01) -> bool:
        """检查 value 是否落在 allowed values 中（浮点容差）。

        委托 ``value_matches(value, self.allowed_values(), tolerance)``。
        artifact 为 None（allowed 为空）时恒返回 ``False``。
        """
        return value_matches(value, self.allowed_values(), tolerance)
