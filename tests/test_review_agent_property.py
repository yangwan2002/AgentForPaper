"""Property 1（评审不可伪造达标）性质测试。

**Property 1: 评审不可伪造达标**
**Validates: Requirements 1.1, 1.3, 1.6**

对任意论文文本与任意迭代轮次，若**生产** provider 的评审输出无法解析为合法
JSON（含可用 `scores`），则产生的 `ReviewRecord.parse_status == FAILED`，且其
`scores` 不会使任一维度达到阈值（即全维度严格低于达标阈值）。

形式化：``∀ run. unparsable(review_output) ⟹ ¬quality_met_by(record)``。

生成策略：用 `hypothesis` 生成随机「不可解析评审文本」。随机文本极少恰好构成
含 `scores` 的合法 JSON，仍以 `assume` 兜底排除偶发的可解析样本，确保前件
（不可解析）成立。
"""

from __future__ import annotations

from hypothesis import assume, given, settings
from hypothesis import strategies as st

from paper_agent.agents.base import AgentContext
from paper_agent.agents.review_agent import ReviewAgent
from paper_agent.config import Config
from paper_agent.providers.llm.base import LLMProvider, LLMResponse, Message
from paper_agent.utils.json_parse import extract_json
from paper_agent.workspace.models import (
    InputMode,
    OutlineNode,
    ParseStatus,
    PaperWorkspace,
    ScoringDimension,
    SectionDraft,
)

# 达标阈值（与 Config 默认一致）：失败路径下全部维度须严格低于该值。
_THRESHOLD = Config().quality_threshold


class _FixedTextProvider(LLMProvider):
    """始终返回固定文本的生产型 provider（每次 complete 都回相同内容）。

    用于驱动 `ReviewAgent` 的生产解析失败路径：`StructuredParser` 在生产模式
    会重试至上限，故 provider 必须对每次调用都返回（同一段）不可解析文本。
    """

    def __init__(self, text: str) -> None:
        self._text = text
        self.calls = 0

    def complete(self, messages: list[Message], **opts) -> LLMResponse:
        self.calls += 1
        return LLMResponse(content=self._text)


def _ws_with_content() -> PaperWorkspace:
    """构造含至少一个非空章节草稿的最小工作区，使 paper_text 非空。"""
    ws = PaperWorkspace(
        workspace_id="w", input_mode=InputMode.GENERATION, topic_background="x"
    )
    ws.outline = [OutlineNode(section_id="intro", title="引言", order=0)]
    ws.section_drafts = {
        "intro": SectionDraft(
            section_id="intro", title="引言", content="这是一段非空的引言草稿内容。"
        )
    }
    return ws


def _is_unparsable_review(text: str) -> bool:
    """判定文本是否「不可解析为含可用 scores 的合法评审 JSON」。"""
    data = extract_json(text)
    if not isinstance(data, dict):
        return True
    scores = data.get("scores")
    if not isinstance(scores, dict):
        return True
    # 四维度中只要有任一可解析为数值，就视为可解析评审（前件不成立）。
    for dim in ScoringDimension:
        value = scores.get(dim.value)
        if value is None:
            continue
        try:
            float(value)
            return False
        except (TypeError, ValueError):
            continue
    return True


@settings(max_examples=200, deadline=None)
@given(
    review_text=st.text(max_size=300),
    prior_iteration=st.integers(min_value=0, max_value=50),
)
def test_property1_unparsable_review_never_meets_quality(
    review_text: str, prior_iteration: int
) -> None:
    """Property 1：生产 provider 下不可解析评审 ⟹ FAILED 且全维度不达阈值。

    **Validates: Requirements 1.1, 1.3, 1.6**
    """
    # 仅保留真正不可解析的样本（排除随机命中合法 scores 的极小概率情形）。
    assume(_is_unparsable_review(review_text))

    provider = _FixedTextProvider(review_text)
    # is_mock=False：生产 provider；ReviewAgent 默认即为生产模式。
    agent = ReviewAgent(provider, is_mock=False)

    ws = _ws_with_content()
    ws.iteration = prior_iteration
    result = agent.run(AgentContext(workspace=ws))
    for mutate in result.mutations:
        mutate(ws)

    record = ws.review_records[-1]

    # 不可伪造达标：解析失败必须如实标记为 FAILED（Req 1.1 / 1.6）。
    assert record.parse_status is ParseStatus.FAILED

    # 四个维度齐备且每一维度分数严格低于达标阈值（Req 1.1 / 1.6）。
    assert set(record.scores.keys()) == set(ScoringDimension)
    assert all(score < _THRESHOLD for score in record.scores.values())

    # 失败记录必须带非空、长度 1–500 的失败原因（Req 1.2 配套）。
    assert 1 <= len(record.unparsed_reason) <= 500
