"""本轮架构修复的回归测试（#1/#3/#7/#9/#14/#15/#19/#20/#16 等）。"""

from __future__ import annotations

import tempfile

from paper_agent.agents.base import AgentContext, AgentResult
from paper_agent.config import Config
from paper_agent.hooks import Hooks
from paper_agent.ingestion import split_draft_into_sections
from paper_agent.observability.usage import UsageTracker
from paper_agent.orchestrator import Orchestrator, PaperRequest
from paper_agent.providers.llm.base import LLMResponse, Message
from paper_agent.providers.llm.mock import MockLLMProvider
from paper_agent.tools.quality_gate import QualityGate, extract_text_citations
from paper_agent.tools.registry import ToolRegistry
from paper_agent.workspace.models import (
    InputMode,
    OutlineNode,
    PaperWorkspace,
    ParseStatus,
    ReferenceEntry,
    ReviewRecord,
    ScoringDimension,
    SectionDraft,
)
from paper_agent.workspace.repository import WorkspaceRepository
from paper_agent.workspace.store import InMemoryStore


# --------------------------------------------------------------------------- #
# #1 / #2：正文 [id] 标注扫描 + _extract_cited 只认显式标注
# --------------------------------------------------------------------------- #


def test_quality_gate_flags_bogus_text_citation():
    """正文里出现未核验的 [id] 标注应被质量闸 high 命中（修订路径不再绕过）。"""
    ws = PaperWorkspace(
        workspace_id="w", input_mode=InputMode.GENERATION, topic_background="x"
    )
    ws.outline = [OutlineNode(section_id="s", title="S", order=0)]
    long = "充分展开的章节正文。" * 20 + "参考 [arxiv:fake9999] 的工作。"
    ws.section_drafts = {"s": SectionDraft(section_id="s", title="S", content=long)}
    report = QualityGate().check(ws)
    assert any(i["type"] == "text_citation_invalid" for i in report.issues)
    assert report.passed is False


def test_quality_gate_clean_text_has_no_text_citation_issues():
    ws = PaperWorkspace(
        workspace_id="w", input_mode=InputMode.GENERATION, topic_background="x"
    )
    ws.outline = [OutlineNode(section_id="s", title="S", order=0)]
    ws.verified_references = [
        ReferenceEntry(id="r1", title="T", authors=["A"], year=2020,
                       source_id="x", verified=True)
    ]
    long = "充分展开的章节正文，引用 [r1] 的工作。" * 10
    ws.section_drafts = {"s": SectionDraft(section_id="s", title="S", content=long,
                                           cited_reference_ids=["r1"])}
    report = QualityGate().check(ws)
    assert not any(i["type"] == "text_citation_invalid" for i in report.issues)
    assert report.passed is True


def test_extract_text_citations_skips_non_citation_brackets():
    """含空格/CJK 的方括号（如 [表格 第1页 #1]）不应被当作引用。"""
    content = "见 [arxiv:1706.03762] 与 [表格 第1页 #1]。"
    assert extract_text_citations(content) == ["arxiv:1706.03762"]


def test_writing_extract_cited_only_matches_bracketed_ids():
    from paper_agent.agents.writing_agent import WritingAgent
    from paper_agent.context.manager import ContextManager

    agent = WritingAgent(MockLLMProvider(), ContextManager(MockLLMProvider()))
    content = "本节参考 [arxiv:1706.03762]，未用其他。"
    available = ["arxiv:1706.03762", "r1", "a"]
    # 短 id "a" 不应靠裸子串误命中。
    assert agent._extract_cited(content, available) == ["arxiv:1706.03762"]


# --------------------------------------------------------------------------- #
# #3：初稿切分 + 草稿修订模式保留初稿
# --------------------------------------------------------------------------- #


def test_split_draft_markdown_headings():
    draft = "# 引言\n背景内容\n# 方法\n做法内容"
    sections = split_draft_into_sections(draft)
    assert [s[1] for s in sections] == ["引言", "方法"]
    assert sections[0][2] == "背景内容"
    assert sections[1][2] == "做法内容"


def test_split_draft_latex_headings():
    draft = "\\section{Intro}\ntext here\n\\subsection{Details}\nmore"
    sections = split_draft_into_sections(draft)
    assert [s[1] for s in sections] == ["Intro", "Details"]


def test_split_draft_no_headings_keeps_full_draft():
    draft = "一段没有标题的初稿\n第二行"
    sections = split_draft_into_sections(draft)
    assert len(sections) == 1
    assert sections[0][0] == "sec_0"
    assert "第二行" in sections[0][2]


def test_plan_agent_preserves_draft_sections_in_revision_mode():
    from paper_agent.agents.plan_agent import PlanAgent

    ws = PaperWorkspace(
        workspace_id="w",
        input_mode=InputMode.DRAFT_REVISION,
        original_draft="# 引言\n背景原文\n# 方法\n做法原文",
    )
    agent = PlanAgent(MockLLMProvider())
    result = agent.run(AgentContext(workspace=ws))
    for m in result.mutations:
        m(ws)
    assert set(ws.draft_sections) == {"sec_0", "sec_1"}
    assert ws.draft_sections["sec_0"] == "背景原文"
    assert ws.draft_sections["sec_1"] == "做法原文"


# --------------------------------------------------------------------------- #
# #14：ToolRegistry upsert
# --------------------------------------------------------------------------- #


def test_registry_upsert_overwrites_without_raising():
    reg = ToolRegistry()
    reg.register("t", "first", lambda **k: 1)
    reg.register("t", "second", lambda **k: 2)  # 同名覆盖，不抛错
    assert reg.call("t") == 2
    assert reg.get("t").description == "second"


# --------------------------------------------------------------------------- #
# #20：Mock 原生 stream
# --------------------------------------------------------------------------- #


def test_mock_stream_yields_content_chunk():
    from paper_agent.providers.llm.base import StreamChunk

    provider = MockLLMProvider()
    chunks = list(provider.stream([Message("user", "hi")]))
    assert chunks
    assert all(isinstance(c, StreamChunk) for c in chunks)
    assert "".join(c.text for c in chunks if c.kind == "content") == "[mock] hi"


# --------------------------------------------------------------------------- #
# #15：Hooks 扩展点
# --------------------------------------------------------------------------- #


class _RecordingHooks(Hooks):
    def __init__(self) -> None:
        self.agent_before: list[str] = []
        self.agent_after: list[str] = []
        self.tool_before: list[str] = []
        self.tool_after: list[tuple[str, BaseException | None]] = []

    def before_agent(self, name, ctx) -> None:
        self.agent_before.append(name)

    def after_agent(self, name, ctx, result) -> None:
        self.agent_after.append(name)

    def before_tool_call(self, name, arguments) -> None:
        self.tool_before.append(name)

    def after_tool_call(self, name, arguments, result, error) -> None:
        self.tool_after.append((name, error))


def test_tool_registry_invokes_tool_hooks():
    hooks = _RecordingHooks()
    reg = ToolRegistry(hooks=hooks)
    reg.register("echo", "d", lambda **k: k["x"])
    assert reg.call("echo", x=1) == 1
    assert hooks.tool_before == ["echo"]
    assert hooks.tool_after == [("echo", None)]


def test_tool_registry_hooks_see_error():
    hooks = _RecordingHooks()
    reg = ToolRegistry(hooks=hooks)
    reg.register("boom", "d", lambda **k: (_ for _ in ()).throw(ValueError("x")))
    try:
        reg.call("boom")
    except ValueError:
        pass
    assert hooks.tool_after == [("boom", None)] or hooks.tool_after[-1][0] == "boom"


def test_orchestrator_invokes_agent_hooks():
    hooks = _RecordingHooks()

    class _Plan:
        name = "plan"

        def run(self, ctx):
            def mut(w):
                w.outline = [OutlineNode(section_id="s", title="S", order=0)]

            return AgentResult(mutations=[mut])

    class _Noop:
        name = "noop"

        def run(self, ctx):
            return AgentResult()

    repo = WorkspaceRepository(InMemoryStore())
    orch = Orchestrator(
        repo=repo,
        plan_agent=_Plan(),
        search_agent=_Noop(),
        writing_agent=_Noop(),
        review_agent=_Noop(),
        config=Config(workspace_dir=tempfile.mkdtemp(), iteration_limit=1),
        hooks=hooks,
    )
    orch.run(PaperRequest(topic_background="t"))
    assert "plan" in hooks.agent_before
    assert "plan" in hooks.agent_after


# --------------------------------------------------------------------------- #
# #19：全局 token 预算超额降级
# --------------------------------------------------------------------------- #


class _OverBudgetTracker(UsageTracker):
    @property
    def total_tokens(self) -> int:
        return 1_000_000  # 远超预算


def test_budget_exceeded_terminates_with_export():
    class _Plan:
        name = "plan"

        def run(self, ctx):
            def mut(w):
                w.outline = [OutlineNode(section_id="s", title="S", order=0)]

            return AgentResult(mutations=[mut])

    class _Noop:
        name = "noop"

        def run(self, ctx):
            return AgentResult()

    repo = WorkspaceRepository(InMemoryStore())
    cfg = Config(workspace_dir=tempfile.mkdtemp(), iteration_limit=5, total_token_budget=1)
    orch = Orchestrator(
        repo=repo,
        plan_agent=_Plan(),
        search_agent=_Noop(),
        writing_agent=_Noop(),
        review_agent=_Noop(),
        config=cfg,
        usage_tracker=_OverBudgetTracker(),
    )
    result = orch.run(PaperRequest(topic_background="t"))
    assert result.terminated_reason == "budget_exceeded"
    assert result.export is not None  # 降级仍导出


# --------------------------------------------------------------------------- #
# #9：停滞早退（可信评审 + 内容连续不变）
# --------------------------------------------------------------------------- #


_STABLE_CONTENT = "这是一段足够长的稳定章节正文内容，用于停滞检测。" * 10


class _StableWriter:
    name = "writer"

    def run(self, ctx):
        def mut(w):
            w.section_drafts["s"] = SectionDraft(
                section_id="s", title="S", content=_STABLE_CONTENT
            )

        return AgentResult(mutations=[mut])


class _LowScoreReviewer:
    """可信（PARSED）但全维度低于阈值，使循环不达标且内容稳定 → 触发停滞。"""

    name = "reviewer"

    def run(self, ctx):
        rec = ReviewRecord(
            iteration=ctx.workspace.iteration + 1,
            scores={d: 5.0 for d in ScoringDimension},
        )

        def mut(w):
            w.review_records.append(rec)

        return AgentResult(mutations=[mut])


class _Plan1:
    name = "plan"

    def run(self, ctx):
        def mut(w):
            w.outline = [OutlineNode(section_id="s", title="S", order=0)]

        return AgentResult(mutations=[mut])


def test_stagnation_terminates_when_content_stable_and_trustworthy():
    repo = WorkspaceRepository(InMemoryStore())
    cfg = Config(workspace_dir=tempfile.mkdtemp(), iteration_limit=5, quality_threshold=8.0)
    orch = Orchestrator(
        repo=repo,
        plan_agent=_Plan1(),
        search_agent=_NoopForStagnation(),
        writing_agent=_StableWriter(),
        review_agent=_LowScoreReviewer(),
        config=cfg,
    )
    result = orch.run(PaperRequest(topic_background="t"))
    assert result.terminated_reason == "stagnation"
    ws = repo.load(result.workspace_id)
    # 第 3 轮触发停滞（sig 连续 2 次不变）。
    assert ws.iteration == 3


class _NoopForStagnation:
    name = "search"

    def run(self, ctx):
        return AgentResult()


# --------------------------------------------------------------------------- #
# #7：评审指出论证充分性不足 → 回流补检索（仅触发一次）
# --------------------------------------------------------------------------- #


class _RecordingSearch:
    name = "search"

    def __init__(self) -> None:
        self.calls = 0

    def run(self, ctx):
        self.calls += 1
        return AgentResult()


class _LowSufficiencyReviewer:
    """可信（PARSED）：仅 SUFFICIENCY 低于阈值，其余达标。"""

    name = "reviewer"

    def run(self, ctx):
        scores = {d: 10.0 for d in ScoringDimension}
        scores[ScoringDimension.SUFFICIENCY] = 5.0
        rec = ReviewRecord(
            iteration=ctx.workspace.iteration + 1, scores=scores
        )

        def mut(w):
            w.review_records.append(rec)

        return AgentResult(mutations=[mut])


def test_retrieval_feedback_triggered_once_when_sufficiency_unmet():
    search = _RecordingSearch()
    repo = WorkspaceRepository(InMemoryStore())
    cfg = Config(workspace_dir=tempfile.mkdtemp(), iteration_limit=5, quality_threshold=8.0)
    orch = Orchestrator(
        repo=repo,
        plan_agent=_Plan1(),
        search_agent=search,
        writing_agent=_StableWriter(),
        review_agent=_LowSufficiencyReviewer(),
        config=cfg,
    )
    result = orch.run(PaperRequest(topic_background="t"))
    # SUFFICIENCY 持续未达标且内容稳定 → 最终停滞；补检索仅触发一次。
    assert search.calls == 1
    assert result.terminated_reason == "stagnation"


# --------------------------------------------------------------------------- #
# #16：共享 paper view
# --------------------------------------------------------------------------- #


def test_assemble_paper_text_shared():
    from paper_agent.workspace.paper_view import assemble_paper_text

    ws = PaperWorkspace(
        workspace_id="w", input_mode=InputMode.GENERATION, topic_background="x"
    )
    ws.outline = [
        OutlineNode(section_id="a", title="A", order=0),
        OutlineNode(section_id="b", title="B", order=1),
    ]
    ws.section_drafts = {
        "a": SectionDraft(section_id="a", title="A", content="AA"),
        "b": SectionDraft(section_id="b", title="B", content="BB"),
    }
    text = assemble_paper_text(ws)
    assert "## [a] A\nAA" in text
    assert "## [b] B\nBB" in text
