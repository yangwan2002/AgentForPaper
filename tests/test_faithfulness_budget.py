"""忠实性筛查单次核验预算（条数封顶）——防大章节逐句串行核验卡住。"""

from __future__ import annotations

from paper_agent.agent_platform.faithfulness_screener import (
    GuardrailFaithfulnessScreener,
)
from paper_agent.workspace.faithfulness import FaithfulnessVerdict
from paper_agent.workspace.models import (
    InputMode,
    OutlineNode,
    PaperWorkspace,
    ReferenceEntry,
    SectionDraft,
)


class _CountingJudge:
    """记录被调用次数；总是返回 SUPPORTED（不拦截，只为数调用次数）。"""

    def __init__(self):
        self.calls = 0

    def judge(self, *, claim, grounding, reference_meta):
        self.calls += 1
        return (FaithfulnessVerdict.SUPPORTED, "ok", "", "supported")


def _ws_with_many_cited_sentences(n: int) -> PaperWorkspace:
    ws = PaperWorkspace(workspace_id="w", input_mode=InputMode.DRAFT_REVISION)
    ws.outline = [OutlineNode(section_id="s1", title="Intro", order=0)]
    # 一篇有很多带已核验引用句子的章节（模拟整章 add_section）。
    ws.verified_references = [
        ReferenceEntry(
            id="1", title="T", authors=["A"], year=2020, source_id="d1",
            verified=True, abstract="x" * 500,  # 足够长的 grounding
        )
    ]
    sentences = " ".join(f"这是第{i}个有支撑的论断 [1]。" for i in range(n))
    ws.section_drafts = {"s1": SectionDraft(section_id="s1", title="Intro", content=sentences)}
    return ws


def test_screen_caps_number_of_judge_calls():
    judge = _CountingJudge()
    screener = GuardrailFaithfulnessScreener(
        judge, min_grounding_chars=1, token_budget=2000,
        max_claims=5, screen_deadline_s=0,  # 只测条数封顶
    )
    ws = _ws_with_many_cited_sentences(30)
    screener.unsupported_reasons(ws, "s1")
    # 30 句但预算只核验 5 句 → judge 最多被调 5 次（不再串行几十次卡住）。
    assert judge.calls == 5


def test_no_cap_when_budget_nonpositive():
    judge = _CountingJudge()
    screener = GuardrailFaithfulnessScreener(
        judge, min_grounding_chars=1, token_budget=2000,
        max_claims=0, screen_deadline_s=0,  # 不限（严格模式）
    )
    ws = _ws_with_many_cited_sentences(8)
    screener.unsupported_reasons(ws, "s1")
    assert judge.calls == 8  # 全部核验
