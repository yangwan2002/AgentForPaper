"""评审智能体测试：LLM JSON 评分路径、确定性回退、章节反馈映射。"""

from __future__ import annotations

import json

from paper_agent.agents.base import AgentContext
from paper_agent.agents.review_agent import ReviewAgent
from paper_agent.providers.llm.mock import MockLLMProvider
from paper_agent.workspace.models import (
    InputMode,
    OutlineNode,
    ParseStatus,
    PaperWorkspace,
    ScoringDimension,
    SectionDraft,
)


def _ws_with_content() -> PaperWorkspace:
    ws = PaperWorkspace(
        workspace_id="w", input_mode=InputMode.GENERATION, topic_background="x"
    )
    ws.outline = [
        OutlineNode(section_id="intro", title="引言", order=0),
        OutlineNode(section_id="method", title="方法", order=1),
    ]
    ws.section_drafts = {
        "intro": SectionDraft(section_id="intro", title="引言", content="引言内容"),
        "method": SectionDraft(section_id="method", title="方法", content="方法内容"),
    }
    return ws


def test_llm_review_parses_scores_and_section_feedback():
    scripted = json.dumps(
        {
            "scores": {"logic": 7.5, "novelty": 6, "sufficiency": 8, "language": 9},
            "suggestions": {"logic": "理顺论证", "novelty": "突出贡献"},
            # 用章节标题作键，应被映射回 section_id。
            "section_feedback": {"方法": "缺少对比实验"},
        }
    )
    agent = ReviewAgent(MockLLMProvider(scripted=[scripted]))
    ws = _ws_with_content()
    result = agent.run(AgentContext(workspace=ws))
    for m in result.mutations:
        m(ws)

    rec = ws.review_records[-1]
    assert rec.scores[ScoringDimension.LOGIC] == 7.5
    assert rec.scores[ScoringDimension.LANGUAGE] == 9.0
    assert rec.suggestions[ScoringDimension.NOVELTY] == "突出贡献"
    # "方法" 标题被映射到 section_id "method"
    assert rec.section_feedback == {"method": "缺少对比实验"}


def test_production_review_fails_when_not_json():
    """生产 provider 下非 JSON 输出 → FAILED，且全维度严格低于达标阈值。

    正确性修复后（Req 1.1/1.6）：生产环境解析失败绝不伪造达标分数，四维度
    一律置于量表下限（0.0），并标记 parse_status=FAILED、附非空失败原因。
    """
    # MockLLMProvider 默认回显非 JSON；is_mock=False 即生产模式。
    agent = ReviewAgent(MockLLMProvider(), is_mock=False)
    ws = _ws_with_content()
    result = agent.run(AgentContext(workspace=ws))
    for m in result.mutations:
        m(ws)

    rec = ws.review_records[-1]
    assert rec.parse_status is ParseStatus.FAILED
    assert set(rec.scores.keys()) == set(ScoringDimension)
    assert all(v < 8.0 for v in rec.scores.values())
    assert rec.unparsed_reason  # 非空失败原因


def test_mock_review_falls_back_when_not_json():
    """Mock/测试 provider 下非 JSON 输出 → MOCK_FALLBACK 确定性评分。

    is_mock=True 时允许确定性回退使反馈循环可终止，但 parse_status 不为
    PARSED，故不会触发达标（Req 1.5）。
    """
    agent = ReviewAgent(MockLLMProvider(), is_mock=True, base_score=6.0)
    ws = _ws_with_content()
    result = agent.run(AgentContext(workspace=ws))
    for m in result.mutations:
        m(ws)

    rec = ws.review_records[-1]
    assert rec.parse_status is ParseStatus.MOCK_FALLBACK
    assert set(rec.scores.keys()) == set(ScoringDimension)
    assert all(v == 6.0 for v in rec.scores.values())


def test_review_fails_when_no_content():
    """无任何草稿内容时（论文文本为空）→ FAILED，全维度严格低于达标阈值。

    Req 1.3：空论文（去首尾空白后长度 0）走失败路径，绝不伪造分数。
    """
    agent = ReviewAgent(MockLLMProvider(), is_mock=False)
    ws = PaperWorkspace(
        workspace_id="w", input_mode=InputMode.GENERATION, topic_background="x"
    )
    result = agent.run(AgentContext(workspace=ws))
    for m in result.mutations:
        m(ws)

    rec = ws.review_records[-1]
    assert rec.parse_status is ParseStatus.FAILED
    assert all(v < 8.0 for v in rec.scores.values())
    assert rec.unparsed_reason
