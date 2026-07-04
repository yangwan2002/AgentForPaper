"""分词器抽象（Req 7.1 / 7.2 / 7.3）。

统一的 token 计量入口，服务于上下文裁剪（`ContextManager`）、用量统计
（`UsageTracker`）与工具循环（`run_tool_loop`）的历史压缩与结果截断。

设计要点：
- `TokenCounter` 为运行期可检查的协议（`count` / `count_messages`）。
- `TiktokenCounter`：基于可选依赖 `tiktoken`，按模型选择编码，编码缺失时
  回退 `cl100k_base`；任何情况下返回非负计数且不抛异常。
- `HeuristicTokenCounter`：无 `tiktoken` 时的回退，约每 2 字符计 1 token、
  向上取整、非空文本至少计 1 token（与历史 `len//2` 口径兼容且不为 0）。
- `build_token_counter()`：`tiktoken` 可导入则用 `TiktokenCounter`，否则启发式。

`tiktoken` 是声明在 `tokenizer` extra 中的可选依赖，本模块惰性导入，缺失时
仍可正常导入并工作。
"""

from __future__ import annotations

import math
from typing import TYPE_CHECKING, Protocol, runtime_checkable

if TYPE_CHECKING:
    from paper_agent.providers.llm.base import Message


# 缺失模型编码时的回退编码。
_FALLBACK_ENCODING = "cl100k_base"
# 启发式估算：约每 N 字符计 1 token。
_HEURISTIC_CHARS_PER_TOKEN = 2


@runtime_checkable
class TokenCounter(Protocol):
    """token 计量抽象。"""

    def count(self, text: str) -> int:
        """返回文本的 token 计数（非负）。"""
        ...

    def count_messages(self, messages: "list[Message]") -> int:
        """返回消息列表的累计 token 计数（非负）。"""
        ...


def _message_text(message: "Message") -> str:
    """把一条消息折叠为用于计量的文本。

    纳入 role 与 content；assistant 携带的 tool_calls（名称 + 参数）也计入，
    因为它们最终都会进入发送给模型的上下文。容错处理缺失字段。
    """
    parts: list[str] = []
    role = getattr(message, "role", None)
    if role:
        parts.append(str(role))
    content = getattr(message, "content", None)
    if content:
        parts.append(str(content))
    tool_calls = getattr(message, "tool_calls", None)
    if tool_calls:
        for call in tool_calls:
            name = getattr(call, "name", "")
            args = getattr(call, "arguments", "")
            if name:
                parts.append(str(name))
            if args:
                parts.append(str(args))
    return "\n".join(parts)


class HeuristicTokenCounter:
    """无 `tiktoken` 时的启发式回退计数器。

    约每 2 字符计 1 token、向上取整；非空文本至少计 1 token；空文本计 0。
    """

    def __init__(self, chars_per_token: int = _HEURISTIC_CHARS_PER_TOKEN) -> None:
        self._chars_per_token = max(1, chars_per_token)

    def count(self, text: str) -> int:
        if not text:
            return 0
        return max(1, math.ceil(len(text) / self._chars_per_token))

    def count_messages(self, messages: "list[Message]") -> int:
        return sum(self.count(_message_text(m)) for m in messages)


class TiktokenCounter:
    """基于 `tiktoken` 的真实分词计数器。

    按模型选择编码；指定模型编码缺失时回退 `cl100k_base`。任何情况下返回
    非负计数且不抛异常（编码失败时降级为启发式）。
    """

    def __init__(self, model: str = "gpt-4o-mini") -> None:
        self._model = model or ""
        self._encoding = self._resolve_encoding(self._model)
        # 编码不可用时的兜底，保证 count 永不抛异常。
        self._heuristic = HeuristicTokenCounter()

    @staticmethod
    def _resolve_encoding(model: str):
        """解析模型对应编码，缺失则回退 cl100k_base，仍失败返回 None。"""
        import tiktoken

        if model:
            try:
                return tiktoken.encoding_for_model(model)
            except (KeyError, ValueError):
                # 指定模型的编码缺失 → 回退默认编码。
                pass
        try:
            return tiktoken.get_encoding(_FALLBACK_ENCODING)
        except (KeyError, ValueError):
            return None

    def count(self, text: str) -> int:
        if not text:
            return 0
        if self._encoding is None:
            return self._heuristic.count(text)
        try:
            return len(self._encoding.encode(text))
        except Exception:
            # 防御式：任何编码异常都降级为启发式，绝不抛出。
            return self._heuristic.count(text)

    def count_messages(self, messages: "list[Message]") -> int:
        return sum(self.count(_message_text(m)) for m in messages)


def _tiktoken_available() -> bool:
    """探测 `tiktoken` 是否可导入（以此为「可用」判定标准）。"""
    try:
        import tiktoken  # noqa: F401
    except Exception:
        return False
    return True


def build_token_counter(model: str = "") -> TokenCounter:
    """构造统一的 token 计数器。

    `tiktoken` 可导入则返回 `TiktokenCounter`，否则回退 `HeuristicTokenCounter`。
    构造过程不抛异常。
    """
    if _tiktoken_available():
        try:
            return TiktokenCounter(model)
        except Exception:
            # 极端情况下 tiktoken 导入成功但初始化异常 → 回退启发式。
            return HeuristicTokenCounter()
    return HeuristicTokenCounter()
