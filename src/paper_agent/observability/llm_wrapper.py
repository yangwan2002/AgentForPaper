"""可观测 LLM provider 装饰器。

用装饰器模式包装任意 LLMProvider：
- 发出请求事件；
- 流式逐块转发增量（LLM_DELTA），实现"边想边输出"的体验；
- 统计 token 用量（UsageTracker）。

无需改动任何智能体即可获得过程可见 + 用量统计。
"""

from __future__ import annotations

from typing import Iterator

from paper_agent.observability.events import Event, EventKind, EventSink
from paper_agent.observability.tracing import span
from paper_agent.observability.usage import UsageTracker
from paper_agent.providers.llm.base import (
    CancellationToken,
    LLMProvider,
    LLMResponse,
    Message,
    StreamChunk,
)


class ObservableLLMProvider(LLMProvider):
    def __init__(
        self,
        inner: LLMProvider,
        sink: EventSink,
        tracker: UsageTracker | None = None,
        *,
        preview_chars: int | None = None,
    ) -> None:
        self._inner = inner
        self._sink = sink
        self._tracker = tracker
        # 请求预览的最大字符数（防用户论文正文经落盘 sink 泄漏）。
        # None → 不截断（向后兼容既有直接构造的测试）；0 → 完全脱敏（空预览）；
        # 正整数 → 截断到该长度。装配层（app）默认传 Config.event_preview_chars。
        self._preview_chars = preview_chars

    def _preview(self, text: str) -> str:
        """按 preview_chars 策略对请求预览脱敏/截断。"""
        if self._preview_chars is None:
            return text
        if self._preview_chars <= 0:
            return "[preview redacted]"
        if len(text) <= self._preview_chars:
            return text
        return text[: self._preview_chars] + "…[truncated]"

    def complete(self, messages: list[Message], **opts) -> LLMResponse:
        # LLM 调用作为一个 span（带耗时）；span 内既有事件自动挂到该 span 下。
        with span(self._sink, "llm.complete"):
            return self._complete_impl(messages, **opts)

    def _complete_impl(self, messages: list[Message], **opts) -> LLMResponse:
        prompt_preview = self._preview(messages[-1].content if messages else "")
        self._sink.emit(Event(EventKind.LLM_REQUEST, message=prompt_preview))

        streamed = {"any": False}

        def on_delta(kind: str, text: str) -> None:
            streamed["any"] = True
            self._sink.emit(
                Event(EventKind.LLM_DELTA, message=text, data={"kind": kind})
            )

        resp = self._inner.complete(messages, on_delta=on_delta, **opts)

        # 非流式（未产生增量）时，补发完整的思考/响应事件。
        if not streamed["any"]:
            if resp.reasoning:
                self._sink.emit(Event(EventKind.LLM_THINKING, message=resp.reasoning))
            self._sink.emit(Event(EventKind.LLM_RESPONSE, message=resp.content))

        # token 统计。
        if self._tracker is not None:
            prompt_text = "\n".join(m.content for m in messages)
            completion_text = (resp.reasoning or "") + resp.content
            pt, ct = self._tracker.add(
                prompt_text,
                completion_text,
                resp.prompt_tokens,
                resp.completion_tokens,
            )
            self._sink.emit(
                Event(
                    EventKind.LLM_USAGE,
                    message=f"本次 ~{pt + ct} tokens（输入{pt}/输出{ct}）",
                    data={"prompt": pt, "completion": ct},
                )
            )
        return resp

    def stream(
        self,
        messages: list[Message],
        *,
        cancel_token: CancellationToken | None = None,
        **opts,
    ) -> Iterator[StreamChunk]:
        # 流式 LLM 调用作为一个 span（带耗时）；生成器耗尽/关闭时 span 闭合。
        with span(self._sink, "llm.stream"):
            yield from self._stream_impl(
                messages, cancel_token=cancel_token, **opts
            )

    def _stream_impl(
        self,
        messages: list[Message],
        *,
        cancel_token: CancellationToken | None = None,
        **opts,
    ) -> Iterator[StreamChunk]:
        """流式补全：逐块转发底层增量并发出观测事件。

        语义约定（升级 Req 5.11/5.12、Req 10.4/10.5）：
        - 对底层产出的**每个**增量发出一个 `LLM_DELTA` 事件，并原样向下游
          转发该 `StreamChunk`（透传生成器，不缓冲全部输出）；
        - 到达终态（正常完成 **或** 取消终止）时，发出**恰好一个** `LLM_USAGE`
          事件。用 try/finally + 幂等标志保证：无论是迭代自然结束、调用方提前
          `break` 关闭生成器（GeneratorExit），还是底层在取消后停止产出，
          `LLM_USAGE` 都只发一次、绝不重复。
        """
        prompt_preview = self._preview(messages[-1].content if messages else "")
        self._sink.emit(Event(EventKind.LLM_REQUEST, message=prompt_preview))

        # 累积增量用于在终态做 token 估算（content + thinking 一并计入输出）。
        content_parts: list[str] = []
        reasoning_parts: list[str] = []
        usage_emitted = {"done": False}

        def emit_usage() -> None:
            # 幂等：仅在首次终态时发出唯一的 LLM_USAGE。
            if usage_emitted["done"]:
                return
            usage_emitted["done"] = True
            if self._tracker is None:
                return
            prompt_text = "\n".join(m.content for m in messages)
            completion_text = "".join(reasoning_parts) + "".join(content_parts)
            pt, ct = self._tracker.add(prompt_text, completion_text)
            self._sink.emit(
                Event(
                    EventKind.LLM_USAGE,
                    message=f"本次 ~{pt + ct} tokens（输入{pt}/输出{ct}）",
                    data={"prompt": pt, "completion": ct},
                )
            )

        try:
            for chunk in self._inner.stream(
                messages, cancel_token=cancel_token, **opts
            ):
                if chunk.kind == "thinking":
                    reasoning_parts.append(chunk.text)
                else:
                    content_parts.append(chunk.text)
                self._sink.emit(
                    Event(
                        EventKind.LLM_DELTA,
                        message=chunk.text,
                        data={"kind": chunk.kind},
                    )
                )
                yield chunk
        finally:
            # 正常完成、取消终止、或调用方提前关闭生成器，均在此发出唯一终态用量。
            emit_usage()
