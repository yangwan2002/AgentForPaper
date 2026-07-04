"""Property 2/3 性质与集成测试（编排器达标守卫 + 终止性）。

**Property 2: 达标蕴含可信评审；Property 3: 终止性**
**Validates: Requirements 2.1, 2.2, 2.6, 2.8**

本测试用一个「会产出不可解析评审」的**脚本化生产 provider** 端到端驱动
`Orchestrator`，断言：

- Property 2（Req 2.1/2.2）：当最近一条评审不可信（生产解析失败 →
  ``parse_status == FAILED``）时，编排器**绝不**误判 ``"quality_met"``，
  而是以可诊断的终止原因 ``iteration_limit_unparsed_review`` 终止（Req 2.6）。
- Property 3（Req 2.8）：反馈循环对任意 ``iteration_limit`` 必在
  ``iteration_limit`` 轮内终止（``ws.iteration <= iteration_limit``）。
- 配套：优雅降级——即便评审不可信，导出仍执行并产出文件（Req 10.2）。

关键设计：使用**非 Mock** 的 fake provider，使 `build_orchestrator` 探测到
``is_mock == False``，从而走**生产**解析路径（评审 ``FAILED`` 而非
``MOCK_FALLBACK``），精确覆盖正确性修复的生产分支。fake provider 每次
``complete`` 都成功返回（不抛异常），故经 `ResilientLLMProvider` 时不触发
任何重试/退避（测试无额外延时）。
"""

from __future__ import annotations

import tempfile

from hypothesis import given, settings
from hypothesis import strategies as st

from paper_agent.app import build_orchestrator
from paper_agent.config import Config
from paper_agent.orchestrator import PaperRequest
from paper_agent.providers.llm.base import LLMProvider, LLMResponse, Message
from paper_agent.providers.llm.mock import MockLLMProvider
from paper_agent.providers.retrieval.mock import MockRetrievalProvider
from paper_agent.workspace.models import OutputFormat, ParseStatus
from paper_agent.workspace.store import InMemoryStore


class _UnparsableReviewProvider(LLMProvider):
    """生产型（非 Mock）provider：对每次调用都返回不可解析为 JSON 的散文。

    - 评审请求：散文无法解析为含 ``scores`` 的合法 JSON，`StructuredParser`
      在生产模式（``is_mock == False``）重试至上限后判定 ``FAILED``。
    - 写作请求：返回同样非空的散文，使章节草稿非空、可被导出（优雅降级）。
    - 始终成功返回、绝不抛异常：经 `ResilientLLMProvider` 时不触发重试/退避。

    刻意**不**继承 `MockLLMProvider`，以使 `build_orchestrator` 的
    ``isinstance(base, MockLLMProvider)`` 探测为假（``is_mock == False``）。
    """

    def __init__(self) -> None:
        self.calls = 0

    def complete(self, messages: list[Message], **opts) -> LLMResponse:
        self.calls += 1
        # 一段绝不构成合法 JSON 的中文散文，既作正文又作（不可解析的）评审输出。
        return LLMResponse(
            content="这是一段无法解析为 JSON 对象的纯文本内容，用于填充章节正文与评审输出。"
        )


def _config(workspace_dir: str, iteration_limit: int = 5) -> Config:
    return Config(
        quality_threshold=8.0,
        iteration_limit=iteration_limit,
        default_output_format=OutputFormat.MARKDOWN,
        workspace_dir=workspace_dir,
        # 本测试刻意用单一非 Mock provider 同时充当 writer/reviewer 以覆盖生产解析
        # 分支；显式允许自评，避免触发生产 fail-closed 装配拒绝。
        allow_self_review=True,
    )


def test_property2_unparsable_review_never_quality_met(tmp_path) -> None:
    """Property 2：生产评审不可解析 ⟹ 不误判 quality_met，以可诊断原因终止。

    **Validates: Requirements 2.1, 2.2, 2.6**
    """
    provider = _UnparsableReviewProvider()
    cfg = _config(str(tmp_path), iteration_limit=5)
    store = InMemoryStore()
    orch = build_orchestrator(
        llm=provider,
        retrieval=MockRetrievalProvider(),
        config=cfg,
        store=store,
    )

    result = orch.run(PaperRequest(topic_background="多智能体协作论文写作"))

    # Property 2（Req 2.1/2.2）：绝不误判达标。
    assert result.terminated_reason != "quality_met"
    # 可诊断终止原因：因最近评审不可信而到达迭代上限（Req 2.6）。
    assert result.terminated_reason == "iteration_limit_unparsed_review"

    # 走的是生产解析失败路径（FAILED），而非 Mock 回退（MOCK_FALLBACK）。
    ws = store.load(result.workspace_id)
    assert ws is not None
    assert ws.review_records
    assert ws.review_records[-1].parse_status is ParseStatus.FAILED

    # 优雅降级（Req 10.2）：即便评审不可信，导出仍执行并产出文件。
    assert result.export is not None
    assert len(result.export.files) >= 1


def test_property3_terminates_within_iteration_limit(tmp_path) -> None:
    """Property 3：反馈循环在 iteration_limit 轮内终止（ws.iteration <= 上限）。

    **Validates: Requirements 2.8**
    """
    provider = _UnparsableReviewProvider()
    cfg = _config(str(tmp_path), iteration_limit=3)
    store = InMemoryStore()
    orch = build_orchestrator(
        llm=provider,
        retrieval=MockRetrievalProvider(),
        config=cfg,
        store=store,
    )

    result = orch.run(PaperRequest(topic_background="主题背景"))

    ws = store.load(result.workspace_id)
    assert ws is not None
    # 终止性：每轮 iteration 恰增 1，必在迭代上限内终止（Req 2.4/2.8）。
    assert ws.iteration <= cfg.iteration_limit
    # 评审始终不可信，应跑满迭代上限后以可诊断原因终止。
    assert ws.iteration == cfg.iteration_limit
    assert result.terminated_reason == "iteration_limit_unparsed_review"


@settings(max_examples=8, deadline=None)
@given(iteration_limit=st.integers(min_value=1, max_value=5))
def test_property3_terminates_for_any_iteration_limit(iteration_limit: int) -> None:
    """Property 3（性质）：对任意 iteration_limit（1..5）均在上限内终止。

    **Validates: Requirements 2.8**

    用 `hypothesis` 变化 ``iteration_limit``，每个样本端到端跑一次 Orchestrator，
    断言终止性与「不误判达标」同时成立。
    """
    with tempfile.TemporaryDirectory() as workspace_dir:
        provider = _UnparsableReviewProvider()
        cfg = _config(workspace_dir, iteration_limit=iteration_limit)
        store = InMemoryStore()
        orch = build_orchestrator(
            llm=provider,
            retrieval=MockRetrievalProvider(),
            config=cfg,
            store=store,
        )

        result = orch.run(PaperRequest(topic_background="主题"))

        ws = store.load(result.workspace_id)
        assert ws is not None
        # 终止性：ws.iteration 不超过迭代上限（Req 2.8）。
        assert ws.iteration <= iteration_limit
        # 不可信评审 ⟹ 绝不误判达标（Property 2 配套）。
        assert result.terminated_reason != "quality_met"


def test_mock_provider_uses_mock_fallback_not_failed(tmp_path) -> None:
    """对照组：纯 Mock provider 走 MOCK_FALLBACK（区别于生产 FAILED）。

    佐证 fake 生产 provider 的设计有效——二者终止原因同为可诊断的
    ``iteration_limit_unparsed_review``，但 parse_status 来源语义不同。
    """
    cfg = _config(str(tmp_path), iteration_limit=2)
    store = InMemoryStore()
    orch = build_orchestrator(
        llm=MockLLMProvider(),
        retrieval=MockRetrievalProvider(),
        config=cfg,
        store=store,
    )

    result = orch.run(PaperRequest(topic_background="主题"))

    ws = store.load(result.workspace_id)
    assert ws is not None
    assert result.terminated_reason == "iteration_limit_unparsed_review"
    assert ws.review_records[-1].parse_status is ParseStatus.MOCK_FALLBACK
