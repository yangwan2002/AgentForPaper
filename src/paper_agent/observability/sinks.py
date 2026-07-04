"""追踪相关的 EventSink 实现：TracingSink / MultiSink / JsonLinesSink。

三者均遵循"可观测绝不拖垮业务"：任何内部异常一律吞掉，不向业务传播（Req 4.1/4.3/2.4）。

- ``TracingSink``：装饰下游 sink，用 ``tracing`` 的当前 trace/span 与时间戳**补全**事件的
  空追踪字段（不覆盖已显式带值的字段，如 span 收尾事件）。
- ``MultiSink``：把事件分发给多个 sink；单个 sink 失败不影响其余。
- ``JsonLinesSink``：把事件逐行写为 JSON，内容级别 ``full/redacted/off`` 控制记录量；
  线程安全（并行落盘加锁）。
"""

from __future__ import annotations

import json
import os
import threading
import time

from paper_agent.observability.events import Event, EventSink
from paper_agent.observability.tracing import current_ids

# 内容级别取值。
CONTENT_FULL = "full"
CONTENT_REDACTED = "redacted"
CONTENT_OFF = "off"
_CONTENT_LEVELS = (CONTENT_FULL, CONTENT_REDACTED, CONTENT_OFF)


class TracingSink(EventSink):
    """装饰下游 sink：emit 时用当前 trace/span 与时间戳补全事件的空追踪字段。"""

    def __init__(self, inner: EventSink) -> None:
        self._inner = inner

    def emit(self, event: Event) -> None:
        try:
            trace_id, span_id = current_ids()
            # 只补"未显式带值"的字段——span 收尾事件已自带 span_id/parent，不被覆盖。
            if not event.trace_id and trace_id:
                event.trace_id = trace_id
            if not event.span_id and span_id:
                event.span_id = span_id
                event.parent_span_id = ""  # 普通事件挂在当前 span 下，无独立 parent
            if not event.ts:
                event.ts = time.time()
        except Exception:  # noqa: BLE001 - 补全失败不影响事件转发
            pass
        try:
            self._inner.emit(event)
        except Exception:  # noqa: BLE001 - 下游失败不拖垮业务
            pass


class MultiSink(EventSink):
    """把事件分发给多个 sink；单个 sink 失败不影响其余（Req 4.3）。"""

    def __init__(self, sinks: list[EventSink]) -> None:
        self._sinks = list(sinks)

    def emit(self, event: Event) -> None:
        for sink in self._sinks:
            try:
                sink.emit(event)
            except Exception:  # noqa: BLE001 - 隔离单 sink 失败
                continue


class JsonLinesSink(EventSink):
    """把事件逐行写为 JSON（JSONL）。内容级别控制 message/data 记录量。

    - ``full``：完整 message + data。
    - ``redacted``：message 截断到 ``redact_chars``；data 中的长字符串字段同样截断。
    - ``off``：不写 message/大文本，仅保留结构与数值型 data。

    线程安全（并行落盘加锁）；任何 I/O 异常吞掉（Req 2.4）；构造时若目录不可建则降级为
    no-op（``_ok=False``），emit 静默丢弃。
    """

    def __init__(
        self,
        path: str | None = None,
        *,
        directory: str | None = None,
        content_level: str = CONTENT_FULL,
        redact_chars: int = 2000,
    ) -> None:
        """两种模式：

        - ``path``：单文件模式，所有事件追加到同一 JSONL。
        - ``directory``：目录模式，按事件的 ``trace_id`` 分文件（``<dir>/<trace_id>.jsonl``，
          空 trace_id 落到 ``untraced.jsonl``）——装配时不必预知运行期 trace_id，实现
          "一次 trace 一份"。

        二者提供其一；都不给则降级 no-op。
        """
        self._path = path
        self._directory = directory
        self._level = content_level if content_level in _CONTENT_LEVELS else CONTENT_FULL
        self._redact_chars = max(0, int(redact_chars))
        self._lock = threading.Lock()
        self._ok = True
        try:
            if directory is not None:
                os.makedirs(directory, exist_ok=True)
            elif path:
                target_dir = os.path.dirname(os.path.abspath(path)) or "."
                os.makedirs(target_dir, exist_ok=True)
            else:
                self._ok = False  # 既无 path 也无 directory
        except Exception:  # noqa: BLE001 - 目录不可建 → 降级 no-op
            self._ok = False

    def _target_path(self, event: Event) -> str | None:
        if self._directory is not None:
            tid = event.trace_id or "untraced"
            # 防御式：trace_id 只含 hex/常规字符，避免路径穿越。
            safe = "".join(c for c in tid if c.isalnum() or c in "-_")[:64] or "untraced"
            return os.path.join(self._directory, f"{safe}.jsonl")
        return self._path

    def emit(self, event: Event) -> None:
        if not self._ok:
            return
        target = self._target_path(event)
        if not target:
            return
        try:
            record = self._to_record(event)
            line = json.dumps(record, ensure_ascii=False, default=str)
        except Exception:  # noqa: BLE001 - 序列化失败不落该条，不影响业务
            return
        try:
            with self._lock:
                with open(target, "a", encoding="utf-8") as fh:
                    fh.write(line + "\n")
        except Exception:  # noqa: BLE001 - I/O 异常吞掉
            return

    def _to_record(self, event: Event) -> dict:
        record = {
            "ts": event.ts or time.time(),
            "trace_id": event.trace_id,
            "span_id": event.span_id,
            "parent_span_id": event.parent_span_id,
            "kind": event.kind.value,
            "duration_ms": event.duration_ms,
        }
        record["message"] = self._render_message(event.message)
        record["data"] = self._render_data(event.data)
        return record

    def _render_message(self, message: str) -> str:
        message = message or ""
        if self._level == CONTENT_OFF:
            return ""
        if self._level == CONTENT_REDACTED and len(message) > self._redact_chars:
            return message[: self._redact_chars] + "…[truncated]"
        return message

    def _render_data(self, data: dict) -> dict:
        data = data or {}
        if self._level == CONTENT_FULL:
            return data
        result: dict = {}
        for key, value in data.items():
            if isinstance(value, str):
                if self._level == CONTENT_OFF:
                    continue  # off：不记字符串内容，仅保留结构/数值
                if len(value) > self._redact_chars:
                    result[key] = value[: self._redact_chars] + "…[truncated]"
                else:
                    result[key] = value
            else:
                # 数值/bool/None 等结构性字段两级别都保留。
                result[key] = value
        return result


__all__ = [
    "TracingSink",
    "MultiSink",
    "JsonLinesSink",
    "CONTENT_FULL",
    "CONTENT_REDACTED",
    "CONTENT_OFF",
]
