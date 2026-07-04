"""流式适配：把 push 风格的 `complete(on_delta=...)` 适配成 pull 风格的 `stream()`。

设计动机（见 design.md 组件 1 / 时序 3，Requirements 5.3–5.6, 5.10）：

具体 provider（`MockLLMProvider`、`OpenAICompatibleProvider` 等）的流式能力是
「推」式的——它们在内部循环里对每个增量调用 `on_delta(kind, text)` 回调。而上层
希望以「拉」式的 `Iterator[StreamChunk]` 消费，并能在增量边界协作式取消。

本模块用一个后台线程运行 `complete(on_delta=...)`，把回调产生的增量喂进一个
无界队列，主协程（生成器）从队列逐块取出并 `yield`。这样：

- 收到**首个**增量即可立即产出首个 `StreamChunk`，无需缓冲全部输出（Req 5.4）；
- 在每个增量边界检查 `cancel_token`，取消后干净停止且视为**正常终态**，
  不抛 `LLMError`（Req 5.5）；首个增量产出前若已取消则直接干净停止（Req 5.6）；
- 既有 provider **无需改动**即可经 `stream_via_complete`/`StreamingMixin` 获得
  `stream()` 能力（Req 5.3）；
- content 增量按序拼接与 `complete().content` 一致（Req 5.10，由 provider 的
  `on_delta` 回调顺序保证，本适配器不重排、不丢弃未取消时的任何增量）。

注意：本适配器是「默认混入」级别的实现。`complete()` 是同步阻塞调用，无法真正
关闭底层连接；取消语义体现为「停止向调用方产出」。需要真正中断底层流的 provider
应自行覆写原生 `stream()`。健壮性（重试）由外层 `ResilientLLMProvider.stream`
负责，本适配器只在未被取消时把底层异常透传出去，交由上层决策。
"""

from __future__ import annotations

import queue
import threading
from typing import TYPE_CHECKING, Iterator, Protocol

# StreamChunk 与 CancellationToken 的规范定义位于 `providers/llm/base`（任务 4.1）。
# 为保证在 4.1 落地前后本模块都能干净导入，这里做防御式导入：
# 优先复用 base 的定义，缺失时回退到 models 的 StreamChunk 并提供等价的
# CancellationToken（鸭子类型兼容：只依赖 `cancel()` 与 `cancelled`）。
try:  # pragma: no cover - 取决于 4.1 是否已落地
    from paper_agent.providers.llm.base import CancellationToken, StreamChunk
except ImportError:  # pragma: no cover - 兼容回退路径
    from paper_agent.workspace.models import StreamChunk

    class CancellationToken:
        """协作式取消令牌：调用方 `cancel()`，生产侧在每个增量边界查 `cancelled`。"""

        def __init__(self) -> None:
            self._cancelled = False

        def cancel(self) -> None:
            self._cancelled = True

        @property
        def cancelled(self) -> bool:
            return self._cancelled


if TYPE_CHECKING:  # 仅类型检查期需要，避免运行期硬依赖顺序问题。
    from paper_agent.providers.llm.base import Message


class _Completable(Protocol):
    """能以 `complete(messages, on_delta=callable, **opts)` 推送增量的对象。"""

    def complete(self, messages: list["Message"], **opts):  # noqa: D401, ANN001
        ...


# 队列哨兵：标记底层 complete() 已结束（无论正常返回还是抛异常）。
_DONE = object()


def stream_via_complete(
    provider: "_Completable",
    messages: list["Message"],
    *,
    cancel_token: "CancellationToken | None" = None,
    **opts,
) -> Iterator[StreamChunk]:
    """基于 `provider.complete(on_delta=...)` 适配出的流式生成器。

    任何实现了 `complete(messages, on_delta=callable, **opts)` 的 provider 都可
    直接经此函数获得 `stream()` 能力，**无需修改 provider 自身**。

    行为契约：
    - 首个增量到达即产出首个 `StreamChunk`，不缓冲全部输出。
    - 每次产出前在增量边界检查 `cancel_token`；一旦取消即停止产出并干净返回，
      取消被视为正常终态（不抛 `LLMError`）。
    - 首个增量产出前若 `cancel_token` 已取消，则直接干净停止。
    - 未被取消时，底层 `complete()` 抛出的异常在全部已产出增量之后透传出来
      （保留已产出增量，不回滚），由外层（如 ResilientLLMProvider）决策。
    """
    # 首增量前已取消：干净停止，连底层调用都不发起。
    if cancel_token is not None and cancel_token.cancelled:
        return

    chunks: "queue.Queue[object]" = queue.Queue()
    captured: dict[str, BaseException] = {}

    def _on_delta(kind: str, text: str) -> None:
        # provider 内部对每个增量调用此回调；立即入队以便主线程尽早产出。
        chunks.put(StreamChunk(kind=kind, text=text))

    def _worker() -> None:
        try:
            provider.complete(messages, on_delta=_on_delta, **opts)
        except BaseException as exc:  # noqa: BLE001 - 透传给消费侧决策
            captured["exc"] = exc
        finally:
            # 无论成功或失败，最终放入哨兵，确保消费侧的阻塞 get() 能解除。
            chunks.put(_DONE)

    worker = threading.Thread(
        target=_worker, name="llm-stream-adapter", daemon=True
    )
    worker.start()

    while True:
        item = chunks.get()  # 阻塞直到首个增量或结束哨兵到达。
        if item is _DONE:
            break
        # 增量边界检查取消：取消则不再产出本块（至多再产出 1 块的上限内，干净停止）。
        if cancel_token is not None and cancel_token.cancelled:
            return
        yield item  # type: ignore[misc]

    # 仅在未被取消时透传底层异常；取消属正常终态，不抛错。
    if (cancel_token is None or not cancel_token.cancelled) and "exc" in captured:
        raise captured["exc"]


class StreamingMixin:
    """为实现了 `complete(on_delta=...)` 的 provider 混入默认 `stream()`。

    用法：让具体 provider 继承本 mixin 即可获得基于 `complete` 适配的流式能力，
    例如 `class MockLLMProvider(StreamingMixin, LLMProvider): ...`。provider 若有
    原生流式，可直接覆写 `stream()`。
    """

    def stream(
        self: "_Completable",
        messages: list["Message"],
        *,
        cancel_token: "CancellationToken | None" = None,
        **opts,
    ) -> Iterator[StreamChunk]:
        return stream_via_complete(
            self, messages, cancel_token=cancel_token, **opts
        )


__all__ = ["StreamingMixin", "stream_via_complete"]
