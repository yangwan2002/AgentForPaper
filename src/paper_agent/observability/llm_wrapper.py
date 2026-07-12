"""可观测 LLM provider 装饰器。

用装饰器模式包装任意 LLMProvider：
- 发出请求事件；
- 流式逐块转发增量（LLM_DELTA），实现"边想边输出"的体验；
- 统计 token 用量（UsageTracker）。

无需改动任何智能体即可获得过程可见 + 用量统计。
"""

from __future__ import annotations

import queue
import threading
import time
from contextvars import copy_context
from typing import Iterator

from paper_agent.context.tokenizer import build_token_counter
from paper_agent.observability.budget import (
    BudgetExceededError,
    call_with_deadline,
    clamp_timeout,
    current_run_budget,
)
from paper_agent.observability.events import Event, EventKind, EventSink
from paper_agent.observability.tracing import span
from paper_agent.observability.usage import UsageTracker
from paper_agent.providers.llm.base import (
    CancellationToken,
    CombinedCancellationToken,
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
        token_cap: int = 0,
        role: str = "unspecified",
        completion_reserve_tokens: int = 4096,
    ) -> None:
        self._inner = inner
        self._sink = sink
        self._tracker = tracker
        # 请求预览的最大字符数（防用户论文正文经落盘 sink 泄漏）。
        # None → 不截断（向后兼容既有直接构造的测试）；0 → 完全脱敏（空预览）；
        # 正整数 → 截断到该长度。装配层（app）默认传 Config.event_preview_chars。
        self._preview_chars = preview_chars
        self._token_cap = max(0, int(token_cap or 0))
        self._role = role or "unspecified"
        self._completion_reserve = max(0, int(completion_reserve_tokens))
        self._counter = tracker.counter if tracker is not None else build_token_counter()

    def _preflight(self, messages: list[Message], opts: dict) -> None:
        """在网络调用前按共享总账预留 prompt/显式输出上限。"""
        counter = self._tracker.counter if self._tracker is not None else self._counter
        prompt_tokens = counter.count("\n".join(m.content for m in messages))
        completion_reserve = max(
            0,
            int(
                opts.get("max_tokens")
                or opts.get("max_completion_tokens")
                or self._completion_reserve
            ),
        )
        total = self._tracker.total_tokens if self._tracker is not None else 0
        calls = self._tracker.calls if self._tracker is not None else 0
        context = current_run_budget()
        if context is not None:
            context.check(
                total_tokens=total,
                calls=calls,
                reserve_tokens=prompt_tokens + completion_reserve,
            )
            return
        # 直接构造 Observable（无 Orchestrator 上下文）时仍执行显式 cap。
        if self._token_cap and total + prompt_tokens + completion_reserve > self._token_cap:
            from paper_agent.observability.budget import BudgetExceededError

            raise BudgetExceededError(
                "tokens",
                limit=self._token_cap,
                observed=total + prompt_tokens + completion_reserve,
            )

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
        remaining = self._apply_deadline_timeout(opts)
        self._preflight(messages, opts)
        prompt_preview = self._preview(messages[-1].content if messages else "")
        self._sink.emit(Event(EventKind.LLM_REQUEST, message=prompt_preview))

        streamed = {"any": False}
        accepting = threading.Event()
        accepting.set()

        def on_delta(kind: str, text: str) -> None:
            # A provider that returns after the hard deadline must not emit late
            # events or affect accounting in the completed main flow.
            if not accepting.is_set():
                return
            streamed["any"] = True
            self._sink.emit(
                Event(EventKind.LLM_DELTA, message=text, data={"kind": kind})
            )

        try:
            resp = call_with_deadline(
                lambda: self._inner.complete(messages, on_delta=on_delta, **opts),
                remaining,
            )
        finally:
            accepting.clear()

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
                role=self._role,
            )
            self._sink.emit(
                Event(
                    EventKind.LLM_USAGE,
                    message=f"本次 ~{pt + ct} tokens（输入{pt}/输出{ct}）",
                    data={"prompt": pt, "completion": ct, "role": self._role},
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
        remaining = self._apply_deadline_timeout(opts)
        self._preflight(messages, opts)
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
            pt, ct = self._tracker.add(
                prompt_text, completion_text, role=self._role
            )
            self._sink.emit(
                Event(
                    EventKind.LLM_USAGE,
                    message=f"本次 ~{pt + ct} tokens（输入{pt}/输出{ct}）",
                    data={"prompt": pt, "completion": ct, "role": self._role},
                )
            )

        deadline_at = (
            time.monotonic() + remaining if remaining != float("inf") else None
        )
        combined_token = CombinedCancellationToken(
            cancel_token, deadline_at=deadline_at
        )

        try:
            chunks = self._deadline_stream(
                messages,
                cancel_token=combined_token,
                caller_token=cancel_token,
                remaining=remaining,
                opts=opts,
            )
            for chunk in chunks:
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
            combined_token.cancel()
            # 正常完成、取消终止、或调用方提前关闭生成器，均在此发出唯一终态用量。
            emit_usage()

    @staticmethod
    def _apply_deadline_timeout(opts: dict) -> float:
        """Pass the remaining global deadline to providers that support timeouts."""
        return clamp_timeout(opts)

    def _deadline_stream(
        self,
        messages: list[Message],
        *,
        cancel_token: CombinedCancellationToken,
        caller_token: CancellationToken | None,
        remaining: float,
        opts: dict,
    ) -> Iterator[StreamChunk]:
        """Pump a finite-deadline stream in a daemon so blocked ``next`` cannot hang."""
        if remaining == float("inf"):
            yield from self._inner.stream(
                messages, cancel_token=cancel_token, **opts
            )
            return

        output: queue.Queue[tuple[str, object]] = queue.Queue()
        stream_holder: list[Iterator[StreamChunk]] = []
        caller_context = copy_context()

        def pump() -> None:
            stream: Iterator[StreamChunk] | None = None
            try:
                stream = iter(
                    self._inner.stream(
                        messages, cancel_token=cancel_token, **opts
                    )
                )
                stream_holder.append(stream)
                for chunk in stream:
                    if cancel_token.cancelled:
                        break
                    output.put(("chunk", chunk))
                output.put(("done", None))
            except BaseException as exc:  # noqa: BLE001 - forwarded to consumer
                output.put(("error", exc))
            finally:
                if stream is not None:
                    close = getattr(stream, "close", None)
                    if callable(close):
                        try:
                            close()
                        except (RuntimeError, ValueError):
                            pass

        threading.Thread(
            target=lambda: caller_context.run(pump),
            name="paper-agent-stream",
            daemon=True,
        ).start()

        deadline_at = time.monotonic() + remaining
        while True:
            wait_s = min(0.05, max(0.0, deadline_at - time.monotonic()))
            if wait_s <= 0:
                cancel_token.cancel()
                self._close_stream_async(stream_holder)
                context = current_run_budget()
                if context is not None:
                    raise context.expire_deadline()
                raise BudgetExceededError(
                    "deadline", limit=remaining, observed=remaining
                )
            if caller_token is not None and caller_token.cancelled:
                cancel_token.cancel()
                self._close_stream_async(stream_holder)
                return
            try:
                kind, value = output.get(timeout=wait_s)
            except queue.Empty:
                continue
            if time.monotonic() >= deadline_at:
                cancel_token.cancel()
                self._close_stream_async(stream_holder)
                context = current_run_budget()
                if context is not None:
                    raise context.expire_deadline()
                raise BudgetExceededError(
                    "deadline", limit=remaining, observed=remaining
                )
            if caller_token is not None and caller_token.cancelled:
                cancel_token.cancel()
                self._close_stream_async(stream_holder)
                return
            if kind == "chunk":
                yield value  # type: ignore[misc]
            elif kind == "done":
                return
            else:
                raise value  # type: ignore[misc]

    @staticmethod
    def _close_stream_async(stream_holder: list[Iterator[StreamChunk]]) -> None:
        """Best-effort close without letting a hostile ``close`` block the caller."""
        if not stream_holder:
            return
        close = getattr(stream_holder[0], "close", None)
        if not callable(close):
            return

        def close_safely() -> None:
            try:
                close()
            except (RuntimeError, ValueError):
                # Python generators cannot be closed while executing; their pump
                # still closes them in ``finally`` once the blocked call returns.
                pass

        threading.Thread(
            target=close_safely, name="paper-agent-stream-close", daemon=True
        ).start()
