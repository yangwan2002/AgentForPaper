"""装配栈集成测试：`Observable(Resilient(base))` 三层叠加（升级 Req 4.8 / 10.3）。

验证当具体 provider 先抛可重试错误、随后成功时，三层装配下：
- 健壮层触发重试并经 sink 发出 `LLM_RETRY` 事件（Req 4.8）；
- 可观测层照常发出请求/响应/用量事件，且 `UsageTracker` 统计齐全（Req 10.3）。

不触网、不真实休眠（monkeypatch `time.sleep` 规避退避延迟）。
"""

from __future__ import annotations

import paper_agent.providers.llm.resilient as resilient_mod
from paper_agent.observability.events import Event, EventKind
from paper_agent.observability.llm_wrapper import ObservableLLMProvider
from paper_agent.observability.usage import UsageTracker
from paper_agent.providers.llm.base import LLMResponse, Message
from paper_agent.providers.llm.resilient import ResilientLLMProvider
from paper_agent.workspace.models import RetryPolicy


class _RecordingSink:
    """记录所有事件，便于断言。"""

    def __init__(self) -> None:
        self.events: list[Event] = []

    def emit(self, event: Event) -> None:
        self.events.append(event)


class _FlakyBaseProvider:
    """具体 provider：前 `fail_times` 次抛可重试错误，之后返回成功响应。"""

    def __init__(self, fail_times: int) -> None:
        self._fail_times = fail_times
        self.calls = 0

    def complete(self, messages, **opts) -> LLMResponse:
        self.calls += 1
        if self.calls <= self._fail_times:
            # 连接重置属可重试错误（is_retryable 命中）。
            raise ConnectionError("远程主机强迫关闭了连接")
        return LLMResponse(content="装配完成", reasoning="先想一下")


def test_observable_resilient_stack_retries_and_tracks_usage(monkeypatch):
    # 规避真实退避休眠，加速测试。
    monkeypatch.setattr(resilient_mod.time, "sleep", lambda _s: None)

    sink = _RecordingSink()
    tracker = UsageTracker()
    base = _FlakyBaseProvider(fail_times=2)  # 失败两次后成功

    resilient = ResilientLLMProvider(
        base, RetryPolicy(max_retries=3, base_backoff=0.0), sink
    )
    observable = ObservableLLMProvider(resilient, sink, tracker)

    resp = observable.complete([Message("user", "请装配")])

    # 三层叠加后仍返回底层成功响应。
    assert resp.content == "装配完成"
    # 底层共调用 3 次：失败两次 + 成功一次（≤ max_retries+1）。
    assert base.calls == 3

    kinds = [e.kind for e in sink.events]

    # Req 4.8：重试触发 LLM_RETRY 事件（两次失败 → 两次重试）。
    retry_events = [e for e in sink.events if e.kind == EventKind.LLM_RETRY]
    assert len(retry_events) == 2
    # 重试事件载荷含重试序号与异常类别，且不泄露完整请求体。
    assert retry_events[0].data["attempt"] == 1
    assert retry_events[1].data["attempt"] == 2
    assert retry_events[0].data["error_type"] == "connection"

    # Req 10.3：可观测层发出请求/响应/用量事件。
    assert EventKind.LLM_REQUEST in kinds
    assert EventKind.LLM_RESPONSE in kinds
    assert EventKind.LLM_USAGE in kinds

    # 用量统计齐全：调用计数自增、总 token > 0。
    assert tracker.calls == 1
    assert tracker.total_tokens > 0


def test_stack_no_retry_event_when_first_call_succeeds(monkeypatch):
    """对照：首次即成功时不应产生任何 LLM_RETRY 事件，但用量仍齐全。"""
    monkeypatch.setattr(resilient_mod.time, "sleep", lambda _s: None)

    sink = _RecordingSink()
    tracker = UsageTracker()
    base = _FlakyBaseProvider(fail_times=0)

    resilient = ResilientLLMProvider(
        base, RetryPolicy(max_retries=3, base_backoff=0.0), sink
    )
    observable = ObservableLLMProvider(resilient, sink, tracker)

    resp = observable.complete([Message("user", "请装配")])

    assert resp.content == "装配完成"
    assert base.calls == 1
    assert not any(e.kind == EventKind.LLM_RETRY for e in sink.events)
    assert EventKind.LLM_USAGE in [e.kind for e in sink.events]
    assert tracker.calls == 1
    assert tracker.total_tokens > 0
