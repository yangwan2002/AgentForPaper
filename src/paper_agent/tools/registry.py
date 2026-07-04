"""工具注册表（工具管理）。

借鉴通用智能体的工具管理思想：将可调用能力统一注册、按名查找、统一调度，
并能导出为 OpenAI 风格的 tools schema 暴露给模型（function calling）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable

from paper_agent.hooks import Hooks


@dataclass
class Tool:
    name: str
    description: str
    handler: Callable[..., Any]
    # JSON Schema（OpenAI function parameters）；默认无参数。
    parameters: dict = field(
        default_factory=lambda: {"type": "object", "properties": {}}
    )

    def to_openai_schema(self) -> dict:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }


class ToolRegistry:
    """工具注册表。

    ``register`` 为 upsert 语义：同名工具再次注册会覆盖旧定义，而非抛错。
    这样调用方可在同一 registry 实例上按需重建/更新工具集，而不必为规避
    「重名抛错」每次构造全新实例（#14 修复：去除脆弱的「全新实例规避重名」用法）。

    #15：``hooks`` 提供 ``before_tool_call`` / ``after_tool_call`` 扩展点，
    在每次 ``call`` 前后触发（默认无 hooks 时无额外开销）。
    """

    def __init__(self, hooks: Hooks | None = None) -> None:
        self._tools: dict[str, Tool] = {}
        self.hooks: Hooks | None = hooks

    def register(
        self,
        name: str,
        description: str,
        handler: Callable[..., Any],
        parameters: dict | None = None,
    ) -> None:
        # upsert：覆盖同名工具，便于在同一实例上更新工具集。
        self._tools[name] = Tool(
            name=name,
            description=description,
            handler=handler,
            parameters=parameters or {"type": "object", "properties": {}},
        )

    def clear(self) -> None:
        """清空已注册工具（便于复用同一实例重建工具集）。"""
        self._tools.clear()

    def get(self, name: str) -> Tool:
        if name not in self._tools:
            raise KeyError(f"未找到工具：{name}")
        return self._tools[name]

    def call(self, name: str, **kwargs) -> Any:
        tool = self.get(name)
        # #15：工具调用前后触发 hooks（审计/限流/缓存等扩展点）。
        if self.hooks is not None:
            self.hooks.before_tool_call(name, kwargs)
        error: BaseException | None = None
        result: Any = None
        try:
            result = tool.handler(**kwargs)
            return result
        except BaseException as exc:  # noqa: BLE001 - 透传给上层，但先记 hook
            error = exc
            raise
        finally:
            if self.hooks is not None:
                self.hooks.after_tool_call(name, kwargs, result, error)

    def list_tools(self) -> list[Tool]:
        return list(self._tools.values())

    def to_openai_schemas(self) -> list[dict]:
        return [t.to_openai_schema() for t in self._tools.values()]
