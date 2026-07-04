"""Property 5 性质测试：重试有界且仅对可重试错误。

**Property 5: 重试有界且仅对可重试错误**
**Validates: Requirements 4.3, 4.4, 4.5, 4.7**

用 hypothesis 生成随机「异常 / 成功」序列，构造一个伪 inner `LLMProvider`，
按序列在每次 `complete()` 调用时抛出可重试 / 不可重试异常或返回成功响应，
然后断言 `ResilientLLMProvider` 满足：

- 底层调用总次数 ≤ `max_retries + 1`（Req 4.7，有界）；
- 首个异常不可重试时，底层恰好调用一次并立即抛出 `LLMError`（Req 4.4）；
- 全部可重试且始终不成功时，底层恰好调用 `max_retries + 1` 次后抛出（Req 4.3）；
- 预算内出现成功时，返回该成功响应；
- 每次重试的计划休眠时长被 `max_backoff * (1 + jitter)` 封顶（Req 4.3）。

另含针对 429 `Retry-After` 的聚焦（非 hypothesis）单元测试（Req 4.5）。

测试通过 monkeypatch `paper_agent.providers.llm.resilient.time.sleep` 避免真实休眠，
使性质测试快速运行。
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from paper_agent.observability.events import Event, EventKind
from paper_agent.providers.llm.base import LLMError, LLMResponse, Message
from paper_agent.providers.llm.resilient import (
    ResilientLLMProvider,
    backoff_delay,
    is_retryable,
    retry_after_seconds,
)
from paper_agent.workspace.models import RetryPolicy


# --- 测试桩：异常类型 ---


class AuthenticationError(Exception):
    """类名命中 `_NON_RETRYABLE_NAMES` 的不可重试鉴权错误。"""


class _StatusError(Exception):
    """携带 HTTP 状态码（及可选响应头）的异常，模拟 SDK 抛出的错误。"""

    def __init__(self, status_code: int, headers: dict | None = None) -> None:
        super().__init__(f"status {status_code}")
        self.status_code = status_code
        if headers is not None:
            self.response = SimpleNamespace(headers=headers)


# 可重试异常规格：429 / 5xx 状态码、连接重置、超时。
_RETRYABLE_SPECS = [
    ("status", 429),
    ("status", 500),
    ("status", 503),
    ("conn", None),
    ("timeout", None),
]

# 不可重试异常规格：400 / 401 状态码、鉴权类名。
_NON_RETRYABLE_SPECS = [
    ("status", 400),
    ("status", 401),
    ("auth", None),
]


def _make_exc(spec: tuple[str, int | None]) -> Exception:
    kind, val = spec
    if kind == "status":
        assert val is not None
        return _StatusError(val)
    if kind == "conn":
        return ConnectionError("远程主机强迫关闭了连接")
    if kind == "timeout":
        return TimeoutError("timed out")
    if kind == "auth":
        return AuthenticationError("invalid api key")
    raise AssertionError(f"未知异常规格：{spec}")


# --- 伪 inner provider ---


class _SequencedInner:
    """按生成序列决定每次 `complete()` 的行为的伪 provider。

    元素形如 `(kind, detail)`，`kind ∈ {"ok", "retryable", "nonretryable"}`。
    调用序号超出序列长度时重复最后一个元素（便于断言「全部可重试」时耗尽重试）。
    """

    def __init__(self, seq: list[tuple[str, tuple[str, int | None] | None]]) -> None:
        self._seq = seq
        self.calls = 0

    def complete(self, messages: list[Message], **opts) -> LLMResponse:
        idx = self.calls
        self.calls += 1
        kind, detail = self._seq[min(idx, len(self._seq) - 1)]
        if kind == "ok":
            return LLMResponse(content=f"ok-{idx}")
        assert detail is not None
        raise _make_exc(detail)


class _RecordingSink:
    """记录所有事件，用于断言重试事件数。"""

    def __init__(self) -> None:
        self.events: list[Event] = []

    def emit(self, event: Event) -> None:
        self.events.append(event)


def _simulate(
    seq: list[tuple[str, tuple[str, int | None] | None]], max_retries: int
) -> tuple[str, int, int | None]:
    """复刻 `ResilientLLMProvider.complete` 的控制流，给出期望结果。

    返回 `(outcome, call_count, ok_index)`：
    - outcome ∈ {"ok", "raise"}；
    - call_count 为底层 `complete` 的期望调用次数；
    - ok_index 为成功时的调用序号（用于校验返回内容），否则 None。
    """
    for attempt in range(max_retries + 1):
        kind, _ = seq[min(attempt, len(seq) - 1)]
        if kind == "ok":
            return "ok", attempt + 1, attempt
        retryable = kind == "retryable"
        if attempt >= max_retries or not retryable:
            return "raise", attempt + 1, None
    raise AssertionError("不可达")


# --- 序列生成策略 ---

_element = st.one_of(
    st.just(("ok", None)),
    st.sampled_from(_RETRYABLE_SPECS).map(lambda s: ("retryable", s)),
    st.sampled_from(_NON_RETRYABLE_SPECS).map(lambda s: ("nonretryable", s)),
)
_sequence = st.lists(_element, min_size=1, max_size=8)


@settings(max_examples=300, deadline=None)
@given(seq=_sequence, max_retries=st.integers(min_value=0, max_value=5))
def test_retry_bounded_and_only_retryable(seq, max_retries):
    """对任意异常序列断言 Property 5 的全部子性质。"""
    sleeps: list[float] = []

    policy = RetryPolicy(
        max_retries=max_retries,
        base_backoff=0.01,
        max_backoff=1.0,
        jitter=0.25,
    )
    inner = _SequencedInner(seq)
    sink = _RecordingSink()
    provider = ResilientLLMProvider(inner, policy=policy, sink=sink)

    expected_outcome, expected_calls, ok_index = _simulate(seq, max_retries)

    # 用上下文管理器 patch，确保每个生成样本都重置（避免 hypothesis 健康检查）。
    with patch(
        "paper_agent.providers.llm.resilient.time.sleep",
        side_effect=lambda d: sleeps.append(d),
    ):
        if expected_outcome == "raise":
            with pytest.raises(LLMError):
                provider.complete([Message("user", "hi")])
        else:
            resp = provider.complete([Message("user", "hi")])
            assert resp.content == f"ok-{ok_index}"

    # Req 4.7：底层调用次数有界。
    assert inner.calls <= max_retries + 1
    assert inner.calls == expected_calls

    # Req 4.4：首个异常不可重试时恰好调用一次并抛出。
    if seq[0][0] == "nonretryable":
        assert inner.calls == 1
        assert expected_outcome == "raise"

    # Req 4.3：全部可重试且从不成功时，恰好用尽 max_retries+1 次。
    if all(e[0] == "retryable" for e in seq):
        assert inner.calls == max_retries + 1
        assert expected_outcome == "raise"

    # 每个非终态尝试恰好触发一次重试事件与一次休眠。
    assert len(sleeps) == inner.calls - 1
    retry_events = [e for e in sink.events if e.kind is EventKind.LLM_RETRY]
    assert len(retry_events) == inner.calls - 1

    # Req 4.3：每次计划休眠时长被 max_backoff*(1+jitter) 封顶。
    cap = policy.max_backoff * (1 + policy.jitter)
    for d in sleeps:
        assert 0.0 <= d <= cap + 1e-9


# --- 聚焦单元测试：429 Retry-After（Req 4.5 / 4.6） ---


def test_retry_after_seconds_parses_numeric_header():
    exc = _StatusError(429, headers={"Retry-After": "5"})
    assert retry_after_seconds(exc) == 5.0


def test_retry_after_only_applies_to_429():
    exc = _StatusError(503, headers={"Retry-After": "5"})
    assert retry_after_seconds(exc) is None


def test_backoff_respects_retry_after_when_within_cap():
    exc = _StatusError(429, headers={"Retry-After": "5"})
    policy = RetryPolicy(base_backoff=1.0, max_backoff=30.0, jitter=0.25)
    # 优先采用 Retry-After（5s），且未超 max_backoff，故精确为 5.0。
    assert backoff_delay(policy, 3, exc) == 5.0


def test_backoff_caps_retry_after_at_max_backoff():
    exc = _StatusError(429, headers={"Retry-After": "120"})
    policy = RetryPolicy(base_backoff=1.0, max_backoff=2.0, jitter=0.25)
    # Retry-After 远超 max_backoff，应被封顶为 max_backoff。
    assert backoff_delay(policy, 0, exc) == 2.0


def test_backoff_falls_back_to_formula_without_header():
    exc = _StatusError(429)  # 无 Retry-After
    policy = RetryPolicy(base_backoff=1.0, max_backoff=30.0, jitter=0.0)
    # jitter=0 → 退避公式 min(1*2^0, 30) = 1.0。
    assert backoff_delay(policy, 0, exc) == 1.0


def test_respect_retry_after_false_ignores_header():
    exc = _StatusError(429, headers={"Retry-After": "5"})
    policy = RetryPolicy(
        base_backoff=1.0, max_backoff=30.0, jitter=0.0, respect_retry_after=False
    )
    # 关闭 respect_retry_after → 忽略响应头，走退避公式（1.0）。
    assert backoff_delay(policy, 0, exc) == 1.0


def test_is_retryable_classification():
    assert is_retryable(_StatusError(429)) is True
    assert is_retryable(_StatusError(500)) is True
    assert is_retryable(_StatusError(503)) is True
    assert is_retryable(ConnectionError("reset")) is True
    assert is_retryable(TimeoutError("t")) is True
    assert is_retryable(_StatusError(400)) is False
    assert is_retryable(_StatusError(401)) is False
    assert is_retryable(AuthenticationError("bad")) is False
