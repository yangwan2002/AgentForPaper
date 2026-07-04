"""追踪上下文：用 ``contextvars`` 维护"当前 trace/span"，实现非侵入的 trace/span 关联。

设计要点（见 spec agent-observability-tracing）：
- **不改散落各处的 emit 调用点**：业务代码继续 ``sink.emit(Event(...))``，由 ``TracingSink``
  在 emit 时读取本模块的当前 trace/span 自动补全事件字段。
- **span 由上下文管理器开**：``with span(...)`` 进入时 push 一个新 span、退出时 pop 并 emit
  一个带 ``duration_ms`` 的收尾事件；``try/finally`` 保证体内抛异常也闭合（Req 4.2）。
- **并行隔离**：``contextvars`` 天然随 ``copy_context`` 隔离，``run_parallel`` 的子任务各自
  运行时其 span 归属各自分支，不串主线程的 span 栈（Property 7）。

无 active trace 时，``current_ids`` 返回空串；``span`` 仍可用（trace_id 为空、span 计时照常），
但一般在 ``new_trace`` 作用域内使用。
"""

from __future__ import annotations

import time
import uuid
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Iterator

from paper_agent.observability.events import Event, EventKind, EventSink


@dataclass
class TraceState:
    """一条 trace 的运行期状态：trace_id + 当前 span 栈（末尾为当前 span）。"""

    trace_id: str
    span_stack: list[str] = field(default_factory=list)

    @property
    def current_span(self) -> str:
        return self.span_stack[-1] if self.span_stack else ""


# 当前追踪状态；None 表示未处于任何 trace 作用域内。
_current: ContextVar[TraceState | None] = ContextVar("paper_agent_trace", default=None)


def _new_id() -> str:
    return uuid.uuid4().hex[:16]


def current_ids() -> tuple[str, str]:
    """返回 (trace_id, span_id)；不在 trace 内则为 ("", "")。"""
    state = _current.get()
    if state is None:
        return "", ""
    return state.trace_id, state.current_span


@contextmanager
def new_trace(trace_id: str | None = None) -> Iterator[str]:
    """开启一条 trace 作用域，返回其 ``trace_id``。

    嵌套调用时复用最外层 trace（同一次运行/对话共享一个 trace_id）——便于
    ``converse`` 复用当前 trace 而非每轮新建。
    """
    existing = _current.get()
    if existing is not None:
        # 已在 trace 内：复用，不新建（保持同一 trace_id）。
        yield existing.trace_id
        return
    tid = trace_id or _new_id()
    token = _current.set(TraceState(trace_id=tid))
    try:
        yield tid
    finally:
        _current.reset(token)


@contextmanager
def span(
    sink: EventSink,
    name: str,
    kind: EventKind = EventKind.SPAN,
    *,
    data: dict | None = None,
) -> Iterator[str]:
    """开启一个 span：push 新 span_id，退出时 pop 并 emit 带 duration 的收尾事件。

    体内抛异常也会在 ``finally`` 中闭合并记录耗时（Req 4.2）。收尾事件**显式**带上本
    span 的 ``span_id`` 与 ``parent_span_id``（此时已 pop，不能依赖 contextvars 补全），
    并在 ``data`` 中标记 ``span="end"``。
    """
    state = _current.get()
    sid = _new_id()
    parent = state.current_span if state is not None else ""
    trace_id = state.trace_id if state is not None else ""
    if state is not None:
        state.span_stack.append(sid)
    start = time.monotonic()
    error = ""
    try:
        yield sid
    except BaseException as exc:  # noqa: BLE001 - 记录后原样抛出，不吞业务异常
        error = f"{type(exc).__name__}: {exc}"
        raise
    finally:
        duration_ms = (time.monotonic() - start) * 1000.0
        if state is not None and state.span_stack and state.span_stack[-1] == sid:
            state.span_stack.pop()
        payload = dict(data or {})
        payload["span"] = "end"
        if error:
            payload["error"] = error
        # 追踪/落盘绝不拖垮业务：收尾 emit 异常一律吞掉。
        try:
            sink.emit(
                Event(
                    kind=kind,
                    message=name,
                    data=payload,
                    trace_id=trace_id,
                    span_id=sid,
                    parent_span_id=parent,
                    ts=time.time(),
                    duration_ms=duration_ms,
                )
            )
        except Exception:  # noqa: BLE001
            pass


__all__ = ["TraceState", "new_trace", "span", "current_ids"]
