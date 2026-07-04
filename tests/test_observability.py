"""可观测层测试：事件发射与端到端事件流。"""

from __future__ import annotations

from paper_agent.app import build_from_config
from paper_agent.config import Config
from paper_agent.observability.events import (
    CallbackSink,
    Event,
    EventKind,
)
from paper_agent.observability.llm_wrapper import ObservableLLMProvider
from paper_agent.orchestrator import PaperRequest
from paper_agent.providers.llm.base import LLMResponse, Message
from paper_agent.providers.llm.mock import MockLLMProvider
from paper_agent.workspace.models import OutputFormat
from paper_agent.workspace.store import InMemoryStore


def test_observable_llm_emits_request_and_response():
    events: list[Event] = []
    wrapped = ObservableLLMProvider(MockLLMProvider(), CallbackSink(events.append))
    resp = wrapped.complete([Message("user", "hello")])
    assert isinstance(resp, LLMResponse)
    kinds = [e.kind for e in events]
    assert EventKind.LLM_REQUEST in kinds
    assert EventKind.LLM_RESPONSE in kinds


class _StreamingProvider:
    """模拟流式 provider：通过 on_delta 逐块回调，并返回真实用量。"""

    def complete(self, messages, on_delta=None, **opts):
        for piece in ["你", "好", "世界"]:
            if on_delta:
                on_delta("content", piece)
        return LLMResponse(
            content="你好世界", prompt_tokens=12, completion_tokens=8
        )


def test_streaming_emits_deltas_and_suppresses_full_response():
    from paper_agent.observability.usage import UsageTracker

    events: list[Event] = []
    tracker = UsageTracker()
    wrapped = ObservableLLMProvider(
        _StreamingProvider(), CallbackSink(events.append), tracker
    )
    resp = wrapped.complete([Message("user", "hi")])
    assert resp.content == "你好世界"

    kinds = [e.kind for e in events]
    # 流式增量被逐块上报。
    assert kinds.count(EventKind.LLM_DELTA) == 3
    # 既然已流式输出，则不再补发完整响应。
    assert EventKind.LLM_RESPONSE not in kinds
    # 用量被统计（真实值）。
    assert EventKind.LLM_USAGE in kinds
    assert tracker.prompt_tokens == 12
    assert tracker.completion_tokens == 8
    assert tracker.estimated is False


def test_usage_tracker_estimates_when_missing():
    from paper_agent.observability.usage import UsageTracker

    t = UsageTracker()
    pt, ct = t.add("a" * 20, "b" * 10)  # 无真实用量 → 估算
    assert pt == 10 and ct == 5         # 约 2 字符/token
    assert t.estimated is True
    assert t.total_tokens == 15
    assert "tokens" in t.summary()


def test_end_to_end_emits_phase_and_iteration_events(tmp_path):
    events: list[Event] = []
    cfg = Config(
        llm_provider="mock",
        retrieval_provider="mock",
        workspace_dir=str(tmp_path),
        default_output_format=OutputFormat.MARKDOWN,
    )
    orch = build_from_config(
        cfg, store=InMemoryStore(), sink=CallbackSink(events.append)
    )
    orch.run(PaperRequest(topic_background="多智能体写作"))

    kinds = {e.kind for e in events}
    assert EventKind.WORKFLOW_START in kinds
    assert EventKind.PHASE in kinds
    assert EventKind.ITERATION in kinds
    assert EventKind.REVIEW_SCORES in kinds
    assert EventKind.WORKFLOW_END in kinds
    # LLM 被包装后应有请求/响应事件。
    assert EventKind.LLM_REQUEST in kinds
