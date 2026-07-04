"""增量忠实性筛查适配器测试（#2）。

验证核心约定：只拦 UNSUPPORTED；SUPPORTED/CANNOT_VERIFY/无引用/无 grounding 一律放行；
判定异常放行；只查目标章节。
"""

from __future__ import annotations

from paper_agent.agent_platform.faithfulness_screener import (
    GuardrailFaithfulnessScreener,
)
from paper_agent.workspace.faithfulness import FaithfulnessVerdict
from paper_agent.workspace.models import (
    InputMode,
    PaperWorkspace,
    ParseStatus,
    ReferenceEntry,
    SectionDraft,
)


class _Judge:
    """按注入的裁决返回；记录调用次数。"""

    def __init__(self, verdict):
        self._verdict = verdict
        self.calls = 0

    def judge(self, *, claim, grounding, reference_meta):
        self.calls += 1
        return (self._verdict, "理由", "片段", ParseStatus.PARSED)


class _RaisingJudge:
    def judge(self, *, claim, grounding, reference_meta):
        raise RuntimeError("judge 炸了")


def _ws_with_cited_section(content, *, with_grounding=True):
    ws = PaperWorkspace(workspace_id="w1", input_mode=InputMode.DRAFT_REVISION)
    ws.section_drafts = {"intro": SectionDraft(section_id="intro", title="引言", content=content)}
    ref = ReferenceEntry(
        id="1", title="Some real paper", authors=["A"], year=2020, source_id="10.1/x",
        verified=True,
        abstract=("这是一段足够长的摘要内容用于组装 grounding 文本，超过最小字符阈值。" * 3)
        if with_grounding else "",
    )
    ws.verified_references = [ref]
    return ws


def _screener(judge):
    return GuardrailFaithfulnessScreener(judge, min_grounding_chars=40, token_budget=4000)


# --- 核心行为 ---------------------------------------------------------------

def test_unsupported_is_blocked():
    ws = _ws_with_cited_section("方法 X 提升了 30% [1]。")
    judge = _Judge(FaithfulnessVerdict.UNSUPPORTED)
    reasons = _screener(judge).unsupported_reasons(ws, "intro")
    assert len(reasons) == 1
    assert "[1]" in reasons[0] and "不支撑" in reasons[0]


def test_supported_passes():
    ws = _ws_with_cited_section("方法 X 提升了 30% [1]。")
    reasons = _screener(_Judge(FaithfulnessVerdict.SUPPORTED)).unsupported_reasons(ws, "intro")
    assert reasons == []


def test_cannot_verify_passes():
    ws = _ws_with_cited_section("方法 X 提升了 30% [1]。")
    reasons = _screener(_Judge(FaithfulnessVerdict.CANNOT_VERIFY)).unsupported_reasons(ws, "intro")
    assert reasons == []


def test_no_citation_no_judge_call():
    # 不带引用的句子：完全不检查、不调判定器（不逼加引用）。
    ws = _ws_with_cited_section("这是本文自己的方法描述，没有任何引用。")
    judge = _Judge(FaithfulnessVerdict.UNSUPPORTED)
    reasons = _screener(judge).unsupported_reasons(ws, "intro")
    assert reasons == []
    assert judge.calls == 0


def test_no_grounding_passes_without_judging():
    # 文献没有可用支撑材料（abstract 空）→ 无法核验 → 放行、不调判定器。
    ws = _ws_with_cited_section("方法 X 提升了 30% [1]。", with_grounding=False)
    judge = _Judge(FaithfulnessVerdict.UNSUPPORTED)
    reasons = _screener(judge).unsupported_reasons(ws, "intro")
    assert reasons == []
    assert judge.calls == 0


def test_unverified_citation_not_judged_here():
    # 引用 [9] 不在已验证库 → 不属本筛查职责（质量闸另管）→ 放行、不调判定器。
    ws = _ws_with_cited_section("某说法 [9]。")
    judge = _Judge(FaithfulnessVerdict.UNSUPPORTED)
    reasons = _screener(judge).unsupported_reasons(ws, "intro")
    assert reasons == []
    assert judge.calls == 0


def test_judge_exception_passes():
    ws = _ws_with_cited_section("方法 X 提升了 30% [1]。")
    reasons = _screener(_RaisingJudge()).unsupported_reasons(ws, "intro")
    assert reasons == []


def test_only_target_section_checked():
    ws = _ws_with_cited_section("方法 X 提升了 30% [1]。")
    ws.section_drafts["other"] = SectionDraft(
        section_id="other", title="其他", content="别的说法 [1]。"
    )
    judge = _Judge(FaithfulnessVerdict.UNSUPPORTED)
    _screener(judge).unsupported_reasons(ws, "intro")
    # 只查了 intro（1 个 pair），没查 other。
    assert judge.calls == 1


# --- 经 GuardrailGate 端到端：拦截 + 放行 -----------------------------------

def test_gate_blocks_unsupported_content_change():
    from paper_agent.agent_platform.apply import commit
    from paper_agent.agent_platform.guardrail_gate import GuardrailGate
    from paper_agent.agent_platform.models import CHANGE_CONTENT, ProposedChange
    from paper_agent.workspace.repository import WorkspaceRepository

    class _Store:
        def __init__(self): self._d = {}
        def load(self, wid):
            import copy
            raw = self._d.get(wid)
            return PaperWorkspace.from_dict(raw) if raw else None
        def save(self, ws):
            import copy
            self._d[ws.workspace_id] = copy.deepcopy(ws.to_dict())

    ws = _ws_with_cited_section("原始内容。")
    repo = WorkspaceRepository(_Store()); repo.create(ws)
    gate = GuardrailGate(faithfulness_screener=_screener(_Judge(FaithfulnessVerdict.UNSUPPORTED)))

    def _mut(w):
        w.section_drafts["intro"].content = "方法 X 提升了 30% [1]。"

    outcome = commit(repo, ws, gate, [ProposedChange(mutation=_mut, kind=CHANGE_CONTENT, section_id="intro")])
    assert outcome.passed is False
    assert repo.load("w1").section_drafts["intro"].content == "原始内容。"  # 未落盘
