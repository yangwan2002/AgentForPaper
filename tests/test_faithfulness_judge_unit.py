"""Unit tests for citation-faithfulness-audit · FaithfulnessJudge 委托解析行为。

任务 6.5（Req 3.2 / 9.2）：验证 ``FaithfulnessJudge.judge`` 将结构化解析完全
委托给注入的 ``StructuredParser``——它

- 以 ``required_keys=("verdict",)`` 调用 ``request_json``（Req 3.2）；
- 恰好调用一次；
- 传入的 ``messages`` 即 ``templates.judge_citation_faithfulness(...)`` 的返回，
  且这些消息携带 claim / grounding / reference_meta（Req 3.1）；
- 使用依赖注入的 parser，不在内部实例化（Req 9.2）——由 SPY 记录的调用足以佐证。
"""

from __future__ import annotations

from paper_agent.agents.citation_faithfulness_agent import FaithfulnessJudge
from paper_agent.parsing.structured_parser import ParseOutcome
from paper_agent.prompts import templates
from paper_agent.workspace.faithfulness import FaithfulnessVerdict
from paper_agent.workspace.models import ParseStatus


class _SpyParser:
    """记录 ``request_json`` 入参的间谍 parser（返回良性 PARSED 结果）。

    与 ``StructuredParser.request_json`` 签名一致：``messages`` 位置参数 +
    keyword-only ``required_keys`` / ``is_mock``。
    """

    def __init__(self) -> None:
        self.calls: list[dict] = []

    def request_json(
        self, messages, *, required_keys=(), is_mock=None
    ) -> ParseOutcome:
        self.calls.append({"messages": messages, "required_keys": required_keys})
        return ParseOutcome(
            status=ParseStatus.PARSED,
            data={"verdict": "supported", "rationale": "", "supporting_snippet": ""},
        )


def test_judge_delegates_parsing_with_verdict_required_key() -> None:
    """judge 以 required_keys=("verdict",) 恰调用一次 request_json，并透传模板消息。"""
    claim = "本文提出的方法在基准上取得了最优效果。"
    grounding = "实验部分：所提方法在三个基准数据集上均超越现有基线。"
    reference_meta = "标题: Some Paper; 年份: 2023; 作者: Alice, Bob"

    spy = _SpyParser()
    judge = FaithfulnessJudge(spy)

    verdict, rationale, snippet, status = judge.judge(
        claim=claim, grounding=grounding, reference_meta=reference_meta
    )

    # 恰好委托一次。
    assert len(spy.calls) == 1
    call = spy.calls[0]

    # required_keys 精确为 ("verdict",)（Req 3.2）。
    assert call["required_keys"] == ("verdict",)

    # messages 即模板返回（judge 不自行解析原始文本，而是把消息交给 parser）。
    expected_messages = templates.judge_citation_faithfulness(
        claim=claim, grounding=grounding, reference_meta=reference_meta
    )
    assert call["messages"] == expected_messages

    # 良性 PARSED 结果如实透传（佐证使用的是注入的 spy，而非内部实例化的 parser）。
    assert verdict is FaithfulnessVerdict.SUPPORTED
    assert status is ParseStatus.PARSED
    assert rationale == ""
    assert snippet == ""


def test_judge_messages_carry_claim_grounding_and_reference_meta() -> None:
    """spy 收到的 messages 文本中包含 claim / grounding / reference_meta（Req 3.1）。"""
    claim = "CLAIM_MARKER_声明句"
    grounding = "GROUNDING_MARKER_依据文本"
    reference_meta = "REFMETA_MARKER_文献元信息"

    spy = _SpyParser()
    judge = FaithfulnessJudge(spy)

    judge.judge(claim=claim, grounding=grounding, reference_meta=reference_meta)

    assert len(spy.calls) == 1
    messages = spy.calls[0]["messages"]

    combined = "\n".join(getattr(m, "content", "") for m in messages)
    assert claim in combined
    assert grounding in combined
    assert reference_meta in combined
