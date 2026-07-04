"""Round 4 修复回归测试：writer/reviewer 模型分离 + 对抗式评审。"""

from __future__ import annotations

import json
import tempfile

from paper_agent.agents.adversarial_review_agent import AdversarialReviewAgent
from paper_agent.agents.base import AgentContext, AgentResult
from paper_agent.app import build_from_config, build_orchestrator
from paper_agent.config import Config
from paper_agent.orchestrator import Orchestrator, PaperRequest
from paper_agent.providers.llm.mock import MockLLMProvider
from paper_agent.providers.retrieval.mock import MockRetrievalProvider
from paper_agent.workspace.models import (
    AdversarialReviewRecord,
    InputMode,
    OutlineNode,
    ParseStatus,
    PaperWorkspace,
    ReviewRecord,
    ScoringDimension,
    SectionDraft,
)
from paper_agent.workspace.repository import WorkspaceRepository
from paper_agent.workspace.store import InMemoryStore


# --------------------------------------------------------------------------- #
# AdversarialReviewAgent 单元行为
# --------------------------------------------------------------------------- #


def _ws_with_content() -> PaperWorkspace:
    ws = PaperWorkspace(
        workspace_id="w", input_mode=InputMode.GENERATION, topic_background="x"
    )
    ws.outline = [OutlineNode(section_id="intro", title="引言", order=0)]
    ws.section_drafts = {
        "intro": SectionDraft(section_id="intro", title="引言", content="x" * 200)
    }
    return ws


def test_adversarial_review_parses_accept_decision():
    """accept 决定且 weaknesses 为空时，记录 decision=accept。"""
    scripted = json.dumps(
        {"decision": "accept", "weaknesses": [], "critical_count": 0}
    )
    agent = AdversarialReviewAgent(MockLLMProvider(scripted=[scripted]))
    ws = _ws_with_content()
    result = agent.run(AgentContext(workspace=ws))
    for m in result.mutations:
        m(ws)
    record = ws.adversarial_records[-1]
    assert record.parse_status is ParseStatus.PARSED
    assert record.decision == "accept"
    assert record.weaknesses == []


def test_adversarial_review_forces_borderline_when_weaknesses_present():
    """模型给了 accept 但同时列了 weakness → 代码层兜底改为 borderline。"""
    scripted = json.dumps(
        {
            "decision": "accept",  # 模型试图"通过"
            "weaknesses": [
                {
                    "section_id": "intro",
                    "category": "claim_unsupported",
                    "severity": "major",
                    "issue": "Intro 的 claim X 没有引用支撑",
                    "suggested_fix": "添加 [refY] 引用",
                }
            ],
        }
    )
    agent = AdversarialReviewAgent(MockLLMProvider(scripted=[scripted]))
    ws = _ws_with_content()
    result = agent.run(AgentContext(workspace=ws))
    for m in result.mutations:
        m(ws)
    record = ws.adversarial_records[-1]
    # 有 weakness 时绝不允许 accept（核心 reward-hack 防线）。
    assert record.decision == "borderline"
    assert len(record.weaknesses) == 1


def test_adversarial_review_filters_empty_issue_entries():
    """空白 issue 字段的 weakness 条目被剔除。"""
    scripted = json.dumps(
        {
            "decision": "reject",
            "weaknesses": [
                {"issue": "  ", "severity": "minor"},  # 空白 → 剔除
                {"issue": "真实 weakness", "severity": "critical"},
            ],
        }
    )
    agent = AdversarialReviewAgent(MockLLMProvider(scripted=[scripted]))
    ws = _ws_with_content()
    result = agent.run(AgentContext(workspace=ws))
    for m in result.mutations:
        m(ws)
    record = ws.adversarial_records[-1]
    assert len(record.weaknesses) == 1
    assert record.critical_count == 1


def test_adversarial_review_invalid_decision_falls_to_reject():
    """非法 decision 取值兜底为 reject。"""
    scripted = json.dumps(
        {"decision": "maybe", "weaknesses": [{"issue": "x", "severity": "minor"}]}
    )
    agent = AdversarialReviewAgent(MockLLMProvider(scripted=[scripted]))
    ws = _ws_with_content()
    result = agent.run(AgentContext(workspace=ws))
    for m in result.mutations:
        m(ws)
    record = ws.adversarial_records[-1]
    assert record.decision == "reject"


def test_adversarial_review_production_failure_marks_failed():
    """生产 provider 输出非 JSON → FAILED，decision=reject。"""
    agent = AdversarialReviewAgent(
        MockLLMProvider(scripted=["这不是 JSON", "也不是 JSON"]),
        is_mock=False,
    )
    ws = _ws_with_content()
    result = agent.run(AgentContext(workspace=ws))
    for m in result.mutations:
        m(ws)
    record = ws.adversarial_records[-1]
    assert record.parse_status is ParseStatus.FAILED
    assert record.decision == "reject"
    assert record.unparsed_reason


def test_adversarial_review_mock_fallback():
    """Mock provider 非 JSON → MOCK_FALLBACK，决定仍为 reject。"""
    agent = AdversarialReviewAgent(MockLLMProvider(), is_mock=True)
    ws = _ws_with_content()
    result = agent.run(AgentContext(workspace=ws))
    for m in result.mutations:
        m(ws)
    record = ws.adversarial_records[-1]
    assert record.parse_status is ParseStatus.MOCK_FALLBACK
    assert record.decision == "reject"  # 即便 mock 也不放水


# --------------------------------------------------------------------------- #
# Orchestrator：对抗式评审参与达标判定
# --------------------------------------------------------------------------- #


class _Plan:
    name = "plan"

    def run(self, ctx):
        def mut(w):
            w.outline = [OutlineNode(section_id="s", title="S", order=0)]

        return AgentResult(mutations=[mut])


class _Writer:
    name = "writer"

    def run(self, ctx):
        def mut(w):
            w.section_drafts["s"] = SectionDraft(
                section_id="s", title="S", content="充分展开的章节正文。" * 20
            )

        return AgentResult(mutations=[mut])


class _PerfectReviewer:
    """主审每轮给满分（PARSED）。"""

    name = "reviewer"

    def run(self, ctx):
        rec = ReviewRecord(
            iteration=ctx.workspace.iteration + 1,
            scores={d: 10.0 for d in ScoringDimension},
            parse_status=ParseStatus.PARSED,
        )

        def mut(w):
            w.review_records.append(rec)

        return AgentResult(mutations=[mut])


class _AcceptAdversarial:
    """对抗审给 accept（找不到 weakness）。"""

    name = "adversarial"

    def run(self, ctx):
        rec = AdversarialReviewRecord(
            iteration=ctx.workspace.iteration + 1,
            decision="accept",
            weaknesses=[],
            critical_count=0,
            parse_status=ParseStatus.PARSED,
        )

        def mut(w):
            w.adversarial_records.append(rec)

        return AgentResult(mutations=[mut])


class _RejectAdversarial:
    """对抗审持续 reject 并列出 weakness。"""

    name = "adversarial"

    def run(self, ctx):
        rec = AdversarialReviewRecord(
            iteration=ctx.workspace.iteration + 1,
            decision="reject",
            weaknesses=[
                {
                    "section_id": "s",
                    "category": "claim_unsupported",
                    "severity": "critical",
                    "issue": "S 的关键 claim 缺证据",
                    "suggested_fix": "补充实验或引用",
                }
            ],
            critical_count=1,
            parse_status=ParseStatus.PARSED,
        )

        def mut(w):
            w.adversarial_records.append(rec)

        return AgentResult(mutations=[mut])


class _Search:
    name = "search"

    def run(self, ctx):
        return AgentResult()


def _orch(adversarial, repo=None):
    repo = repo or WorkspaceRepository(InMemoryStore())
    cfg = Config(workspace_dir=tempfile.mkdtemp(), iteration_limit=3, quality_threshold=8.0)
    return Orchestrator(
        repo=repo,
        plan_agent=_Plan(),
        search_agent=_Search(),
        writing_agent=_Writer(),
        review_agent=_PerfectReviewer(),
        config=cfg,
        adversarial_review_agent=adversarial,
    )


def test_orchestrator_quality_met_requires_adversarial_accept():
    """主审满分但对抗审 reject → 不达标，跑到 iteration_limit。"""
    orch = _orch(_RejectAdversarial())
    result = orch.run(PaperRequest(topic_background="t"))
    # 主审满分（gate 通过）+ 对抗审 reject → 不达标。
    assert result.terminated_reason != "quality_met"


def test_orchestrator_quality_met_when_both_reviewers_pass():
    """主审满分 AND 对抗审 accept → 达标。"""
    orch = _orch(_AcceptAdversarial())
    result = orch.run(PaperRequest(topic_background="t"))
    assert result.terminated_reason == "quality_met"


def test_orchestrator_without_adversarial_keeps_legacy_behavior():
    """未装配对抗审 → 沿用旧判据（主审 + gate 即可），向后兼容。"""
    orch = _orch(adversarial=None)
    result = orch.run(PaperRequest(topic_background="t"))
    assert result.terminated_reason == "quality_met"


def test_adversarial_weaknesses_feed_into_edits():
    """对抗审 critical weakness 应进入下一轮 gate_fixes。"""
    repo = WorkspaceRepository(InMemoryStore())
    orch = _orch(_RejectAdversarial(), repo=repo)
    result = orch.run(PaperRequest(topic_background="t"))
    ws = repo.load(result.workspace_id)
    # 跑到 iteration_limit，期间 adversarial 反复 reject，weaknesses 已多次注入 edits。
    assert ws.adversarial_records
    assert all(ar.decision == "reject" for ar in ws.adversarial_records)


# --------------------------------------------------------------------------- #
# build_from_config: reviewer_llm_* 配置触发独立 LLM 实例
# --------------------------------------------------------------------------- #


def test_default_config_writer_and_reviewer_share_mock_llm():
    """零配置下 writer 与 reviewer 共享 mock LLM 实例（向后兼容）。"""
    from paper_agent.providers.factory import build_reviewer_llm_provider

    cfg = Config(llm_provider="mock", retrieval_provider="mock")
    assert build_reviewer_llm_provider(cfg) is None


def test_reviewer_llm_override_returns_independent_provider():
    """配置了 reviewer_llm_model → factory 返回独立 provider 实例。

    用 mock 触发：reviewer_llm_provider="mock" 即可（不需真实 endpoint）。
    """
    from paper_agent.providers.factory import build_reviewer_llm_provider

    cfg = Config(
        llm_provider="mock",
        retrieval_provider="mock",
        reviewer_llm_provider="mock",
    )
    provider = build_reviewer_llm_provider(cfg)
    assert provider is not None
    # 与 writer LLM 是不同实例（自评隔离）。
    from paper_agent.providers.factory import build_llm_provider

    writer_provider = build_llm_provider(cfg)
    assert provider is not writer_provider


def test_full_assembly_with_mock_runs_end_to_end(tmp_path):
    """默认装配（含对抗审）在 mock 下仍能跑到终止 + 导出。"""
    orch = build_from_config(
        Config(workspace_dir=str(tmp_path), iteration_limit=2),
        store=InMemoryStore(),
    )
    result = orch.run(PaperRequest(topic_background="多智能体写作"))
    # Mock 评审为 MOCK_FALLBACK（不可信）→ 跑到上限以可诊断原因终止。
    assert result.terminated_reason == "iteration_limit_unparsed_review"
    assert result.export is not None
