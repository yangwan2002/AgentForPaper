"""集成测试：忠实性审计在 Observable LLM 栈下的可观测性（任务 10.3）。

验证当 ``CitationFaithfulnessAgent`` / ``FaithfulnessJudge`` 构建在与
``app.build_orchestrator`` 相同的 ``Observable(Resilient(base))`` 调用栈之上、
并注入共享的 ``UsageTracker`` 时：

1. 判定器发起的 LLM 调用被可观测层自动记录——``UsageTracker`` 计数/总 token
   前进，且 sink 捕获到 ``LLM_USAGE`` 事件（Req 7.2）。
2. 智能体自身经 sink 发出的观测日志（``AGENT_LOG``）受长度上限约束（截断到
   ``_OBS_SNIPPET_MAX``），且任何事件都不泄露植入的「密钥」哨兵（Req 7.5）。

不触网、不真实休眠：底层用确定性 ``MockLLMProvider``，判定 JSON 直接由 mock
产出，故判定器被真实调用（→ 触发 LLM_REQUEST/RESPONSE/USAGE），无需真实模型。
"""

from __future__ import annotations

import json
import os

from paper_agent.agents.base import AgentContext
from paper_agent.agents.citation_faithfulness_agent import (
    _OBS_SNIPPET_MAX,
    CitationFaithfulnessAgent,
    FaithfulnessJudge,
)
from paper_agent.app import _wrap_llm_stack
from paper_agent.observability.events import CallbackSink, Event, EventKind
from paper_agent.observability.usage import UsageTracker
from paper_agent.parsing.structured_parser import StructuredParser
from paper_agent.providers.llm.base import LLMResponse, Message
from paper_agent.providers.llm.mock import MockLLMProvider
from paper_agent.workspace.faithfulness import FaithfulnessVerdict
from paper_agent.workspace.models import (
    InputMode,
    OutputFormat,
    PaperWorkspace,
    ReferenceEntry,
    RetryPolicy,
    SectionDraft,
)

# 植入的「密钥」哨兵：模拟环境中的 API key。审计的任何 emitted 事件都绝不应包含它。
_SECRET_API_KEY = "sk-FAKE-SUPER-SECRET-DO-NOT-LEAK-0123456789abcdef"

# 判定器读取的确定性 JSON 裁决（合法结构 → StructuredParser 判为 PARSED）。
_JUDGE_JSON = json.dumps(
    {
        "verdict": "supported",
        "rationale": "grounding 明确支撑该声明。",
        "supporting_snippet": "the proposed method improves retrieval accuracy",
    }
)


class _JsonMockProvider(MockLLMProvider):
    """始终返回合法忠实性裁决 JSON 的 Mock provider（不论调用次数/入参）。

    继承自 ``MockLLMProvider`` 以复用其 ``stream`` 及回调语义；仅覆盖 ``complete``
    使判定路径拿到可解析的结构化输出，从而判定器被**真实**调用并产生用量。
    """

    def complete(self, messages: list[Message], **opts) -> LLMResponse:  # noqa: D401
        opts.pop("on_delta", None)
        self.calls.append(messages)
        return LLMResponse(content=_JUDGE_JSON)


def _make_workspace() -> PaperWorkspace:
    """构造含「一条已验证文献 + 在正文被引用」的工作区，保证判定器被调用。"""
    ws = PaperWorkspace(
        workspace_id="ws-obs",
        input_mode=InputMode.GENERATION,
        output_format=OutputFormat.MARKDOWN,
        topic_background="可观测性集成测试",
    )
    ws.verified_references = [
        ReferenceEntry(
            id="ref1",
            title="Efficient Transformers for Retrieval",
            authors=["A. Author", "B. Writer"],
            year=2021,
            source_id="ref1",
            source="arxiv",
            verified=True,
            abstract=(
                "We present a retrieval method that improves accuracy on standard "
                "benchmarks while reducing latency. The approach combines dense "
                "encoders with a lightweight re-ranking stage and is evaluated "
                "across several public datasets."
            ),
        )
    ]
    ws.section_drafts = {
        "intro": SectionDraft(
            section_id="intro",
            title="Introduction",
            content=(
                "The proposed method improves retrieval accuracy on the benchmark "
                "[ref1]. This establishes a strong baseline for later sections."
            ),
        )
    }
    return ws


def test_faithfulness_audit_records_usage_and_bounds_event_text():
    """Validates: Requirements 7.2, 7.5"""
    # 环境中植入一个假密钥；审计的任何 emitted 事件都不得包含它。
    os.environ["PAPER_AGENT_TEST_API_KEY"] = _SECRET_API_KEY
    try:
        events: list[Event] = []
        sink = CallbackSink(events.append)
        tracker = UsageTracker()

        # 与 app.build_orchestrator 完全一致的 reviewer LLM 栈：
        # Observable(Resilient(base), sink, tracker)。
        base = _JsonMockProvider()
        policy = RetryPolicy(
            max_retries=3, base_backoff=0.0, max_backoff=1.0, jitter=0.0
        )
        reviewer_stack = _wrap_llm_stack(base, policy, sink, tracker)

        # reviewer_parser 绑定到 Observable 栈；判定属评审型任务，用量自动纳入统计。
        reviewer_parser = StructuredParser(reviewer_stack, is_mock=True)
        agent = CitationFaithfulnessAgent(
            FaithfulnessJudge(reviewer_parser),
            min_grounding_chars=10,
            token_budget=2000,
            is_mock=True,
            sink=sink,
        )

        ws = _make_workspace()

        # 记录审计前的用量基线。
        calls_before = tracker.calls
        tokens_before = tracker.total_tokens

        result = agent.run(AgentContext(workspace=ws))

        # 单独在干净工作区上应用 mutation 读取报告。
        scratch = _make_workspace()
        for mut in result.mutations:
            mut(scratch)
        report = list(scratch.citation_faithfulness)

        # 前置：判定器确实被调用（已验证对被真实判定，而非短路）。
        assert len(base.calls) >= 1, "判定器未发起任何 LLM 调用"
        verified_findings = [f for f in report if not f["unverified_reference"]]
        assert verified_findings, "没有产生已验证对的判定发现"
        assert verified_findings[0]["verdict"] == FaithfulnessVerdict.SUPPORTED.value

        # 断言 1（Req 7.2）：Observable 包装自动记录了判定器的 LLM 用量。
        assert tracker.calls > calls_before
        assert tracker.total_tokens > tokens_before
        usage_events = [e for e in events if e.kind is EventKind.LLM_USAGE]
        assert usage_events, "未捕获到 LLM_USAGE 事件"

        # 断言 2（Req 7.5）：智能体自身 AGENT_LOG 事件受长度上限约束。
        agent_logs = [
            e
            for e in events
            if e.kind is EventKind.AGENT_LOG
            and e.data.get("agent") == agent.name
        ]
        assert agent_logs, "未捕获到智能体的 AGENT_LOG 事件"
        for e in agent_logs:
            assert len(e.message) <= _OBS_SNIPPET_MAX, (
                f"AGENT_LOG 事件文本超出上限 {_OBS_SNIPPET_MAX}：{len(e.message)}"
            )

        # 断言 2（续）：任何 emitted 事件（message 或 data）都不泄露植入的密钥。
        for e in events:
            assert _SECRET_API_KEY not in e.message
            assert _SECRET_API_KEY not in json.dumps(e.data, default=str)
    finally:
        os.environ.pop("PAPER_AGENT_TEST_API_KEY", None)


if __name__ == "__main__":  # pragma: no cover
    import pytest

    pytest.main([__file__, "-q"])
