"""LLM 重试逻辑测试（不触网，mock 底层 client）。

重试/退避/429 处理已统一上提至 ``ResilientLLMProvider``；具体 provider
（``OpenAICompatibleProvider``）不再内部重试，而是把原始异常透传给 Resilient
统一治理（避免双重重试，并使状态码/Retry-After 判定生效）。

因此本测试覆盖**生产装配栈** ``ResilientLLMProvider(OpenAICompatibleProvider)``：
底层 client 抛原始异常 → Resilient 据异常类别决定是否重试。
"""

from __future__ import annotations

import time
from types import SimpleNamespace

import pytest

from paper_agent.observability.budget import (
    BudgetExceededError,
    RunBudgetContext,
    activate_run_budget,
    reset_run_budget,
)
from paper_agent.providers.llm.base import LLMError, Message
from paper_agent.providers.llm.openai_compatible import OpenAICompatibleProvider
from paper_agent.providers.llm.resilient import ResilientLLMProvider
from paper_agent.workspace.models import RetryPolicy


class AuthenticationError(Exception):
    """模拟不可重试的鉴权错误（类名命中 _NON_RETRYABLE_NAMES）。"""


def _make_stack() -> tuple[OpenAICompatibleProvider, ResilientLLMProvider]:
    # 构造不触网；具体 provider 不再内部重试，重试由 Resilient 承担。
    base = OpenAICompatibleProvider(
        model="m", api_key="k", base_url="http://local",
    )
    # base_backoff=0、jitter=0 → 退避为 0，测试快速且确定性。
    resilient = ResilientLLMProvider(
        base, RetryPolicy(max_retries=3, base_backoff=0.0, jitter=0.0)
    )
    return base, resilient


def _fake_response(text: str):
    msg = SimpleNamespace(content=text, reasoning_content=None)
    return SimpleNamespace(choices=[SimpleNamespace(message=msg)])


def _inject_client(provider: OpenAICompatibleProvider, create_fn) -> None:
    provider._client = SimpleNamespace(
        chat=SimpleNamespace(completions=SimpleNamespace(create=create_fn))
    )


def test_retries_then_succeeds():
    base, resilient = _make_stack()
    calls = {"n": 0}

    def flaky_create(**kwargs):
        calls["n"] += 1
        if calls["n"] < 3:
            raise ConnectionError("远程主机强迫关闭了连接")  # 可重试
        return _fake_response("ok")

    _inject_client(base, flaky_create)
    resp = resilient.complete([Message("user", "hi")])
    assert resp.content == "ok"
    assert calls["n"] == 3  # 前两次失败重试，第三次成功


def test_non_retryable_fails_fast():
    base, resilient = _make_stack()
    calls = {"n": 0}

    def auth_fail(**kwargs):
        calls["n"] += 1
        raise AuthenticationError("invalid api key")

    _inject_client(base, auth_fail)
    with pytest.raises(LLMError):
        resilient.complete([Message("user", "hi")])
    assert calls["n"] == 1  # 不可重试，只尝试一次


def test_exhausts_retries_then_raises():
    base, resilient = _make_stack()
    calls = {"n": 0}

    def always_fail(**kwargs):
        calls["n"] += 1
        raise TimeoutError("timed out")

    _inject_client(base, always_fail)
    with pytest.raises(LLMError):
        resilient.complete([Message("user", "hi")])
    assert calls["n"] == 4  # 1 次初始 + 3 次重试


def test_provider_no_longer_retries_internally():
    """具体 provider 不再内部重试：原始异常直接透传（由 Resilient 承担）。"""
    base = OpenAICompatibleProvider(
        model="m", api_key="k", base_url="http://local",
    )
    calls = {"n": 0}

    def always_fail(**kwargs):
        calls["n"] += 1
        raise TimeoutError("timed out")

    _inject_client(base, always_fail)
    # 不经 Resilient 直接调用 → 异常原样透传，不重试。
    with pytest.raises(TimeoutError):
        base.complete([Message("user", "hi")])
    assert calls["n"] == 1


def test_resilient_backoff_does_not_cross_global_deadline():
    base = OpenAICompatibleProvider(
        model="m", api_key="k", base_url="http://local"
    )
    calls = {"n": 0}

    def always_fail(**kwargs):
        calls["n"] += 1
        raise TimeoutError("timed out")

    _inject_client(base, always_fail)
    resilient = ResilientLLMProvider(
        base,
        RetryPolicy(
            max_retries=3,
            base_backoff=0.2,
            max_backoff=0.2,
            jitter=0.0,
        ),
    )
    budget_token = activate_run_budget(RunBudgetContext(duration_cap_s=0.05))
    started = time.monotonic()
    try:
        with pytest.raises(BudgetExceededError, match="deadline"):
            resilient.complete([Message("user", "hi")], timeout=10)
    finally:
        reset_run_budget(budget_token)

    assert time.monotonic() - started < 0.15
    assert calls["n"] == 1
