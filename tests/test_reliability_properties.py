"""agent-reliability-and-subagents 设计属性测试（Task 7，Property 1-9）。

用 hypothesis 为设计文档的 9 条正确性属性各写至少一条 property，并覆盖两条端到端
对照（可自愈 / 不可自愈）与向后兼容回归。
"""

from __future__ import annotations

import copy
import tempfile

from hypothesis import given, settings
from hypothesis import strategies as st

from paper_agent.agent_platform.acceptance import (
    AcceptanceChecker,
    AcceptanceLoop,
    TaskRequirements,
    check_citation_closure,
    detect_mojibake,
)
from paper_agent.agent_platform.models import AgentSession, WritingTask
from paper_agent.agent_platform.subagents import build_curated_context
from paper_agent.export.citation_closure import cited_references
from paper_agent.export.markdown import MarkdownExporter
from paper_agent.workspace.models import (
    InputMode,
    OutlineNode,
    OutputFormat,
    PaperWorkspace,
    ReferenceEntry,
    SectionDraft,
)


class _FakeSession:
    def __init__(self, ws):
        self.workspace = ws
        self.task = WritingTask("t")
        self.transcript = []

    def record(self, *a, **k):
        pass


def _ws(n_refs, cited_ids, *, content_extra="") -> PaperWorkspace:
    ws = PaperWorkspace(workspace_id="w", input_mode=InputMode.GENERATION)
    ws.outline = [OutlineNode(section_id="s1", title="Intro", order=0)]
    ws.verified_references = [
        ReferenceEntry(id=str(i), title=f"T{i}", authors=[f"A{i}"], year=2020,
                       source_id=f"d{i}", verified=True)
        for i in range(1, n_refs + 1)
    ]
    body = " ".join(f"[{c}]" for c in cited_ids) + content_extra
    ws.section_drafts = {"s1": SectionDraft(section_id="s1", title="Intro", content="正文 " + body)}
    return ws


# --------------------------------------------------------------------------- #
# Property 1: 验收确定性与可复现
# --------------------------------------------------------------------------- #

@settings(max_examples=60)
@given(st.text(max_size=200))
def test_p1_detect_mojibake_deterministic(text):
    a = detect_mojibake(text)
    b = detect_mojibake(text)
    assert a == b


@settings(max_examples=60)
@given(st.integers(min_value=0, max_value=6), st.integers(min_value=0, max_value=6))
def test_p1_citation_closure_deterministic(n_refs, k):
    cited = [str(i) for i in range(1, k + 1)]
    ws = _ws(n_refs, cited)
    r1 = check_citation_closure(ws)
    r2 = check_citation_closure(ws)
    assert (r1.ok, r1.detail) == (r2.ok, r2.detail)


# --------------------------------------------------------------------------- #
# Property 2: 引用闭合（导出参考文献表 = 被引用集合）
# --------------------------------------------------------------------------- #

@settings(max_examples=60)
@given(
    n_refs=st.integers(min_value=0, max_value=6),
    cited=st.lists(st.integers(min_value=1, max_value=6), max_size=6, unique=True),
)
def test_p2_citation_closure_export(n_refs, cited):
    cited_ids = [str(c) for c in cited]
    ws = _ws(n_refs, cited_ids)
    valid_cited = {c for c in cited_ids if 1 <= int(c) <= n_refs}

    refs = cited_references(ws)
    assert {r.id for r in refs} == valid_cited

    with tempfile.TemporaryDirectory() as d:
        result = MarkdownExporter().export(ws, d)
        text = open(result.files[0], encoding="utf-8").read()
    # 被引用文献出现在参考文献表；未被引用者不出现。
    for r in ws.verified_references:
        line = f". {r.title}. {r.source}:{r.source_id}"
        if r.id in valid_cited:
            assert line in text
        else:
            assert line not in text


# --------------------------------------------------------------------------- #
# Property 3: 不静默交付坏结果
# --------------------------------------------------------------------------- #

@settings(max_examples=40)
@given(st.integers(min_value=1, max_value=4))
def test_p3_dangling_not_silently_delivered(n_dangle):
    dangling = [str(100 + i) for i in range(n_dangle)]
    ws = _ws(2, ["1"] + dangling)  # [1] 有效，其余悬空
    session = _FakeSession(ws)
    # heal 无效 → 必然进入 unresolved 上报，绝不标记为已交付。
    loop = AcceptanceLoop(
        AcceptanceChecker(), export_fn=lambda w: [], heal_fn=lambda s, f: None
    )
    outcome = loop.run(session, TaskRequirements(), max_heal_rounds=1)
    assert outcome.delivered is False
    assert outcome.unresolved


# --------------------------------------------------------------------------- #
# Property 4: 有界自愈
# --------------------------------------------------------------------------- #

@settings(max_examples=40)
@given(st.integers(min_value=0, max_value=5))
def test_p4_bounded_self_heal(max_rounds):
    ws = _ws(2, ["1", "999"])  # 恒有悬空
    session = _FakeSession(ws)
    calls = {"n": 0}

    def heal(s, f):
        calls["n"] += 1  # 无效修正

    loop = AcceptanceLoop(AcceptanceChecker(), export_fn=lambda w: [], heal_fn=heal)
    outcome = loop.run(session, TaskRequirements(), max_heal_rounds=max_rounds)
    assert outcome.heal_rounds <= max_rounds
    assert calls["n"] <= max_rounds


# --------------------------------------------------------------------------- #
# Property 5: 自愈不破坏（无可行修正路径时不触发自愈，工作区不变）
# --------------------------------------------------------------------------- #

@settings(max_examples=30)
@given(st.integers(min_value=1, max_value=3))
def test_p5_no_heal_for_blocking_preserves_workspace(max_rounds):
    ws = _ws(1, ["1"])
    garbled = "这是中文".encode("utf-8").decode("latin-1")
    session = _FakeSession(ws)
    heal_calls = {"n": 0}

    with tempfile.TemporaryDirectory() as d:
        import os
        path = os.path.join(d, "out.md")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(garbled)
        before = copy.deepcopy(ws.to_dict())

        def heal(s, f):
            heal_calls["n"] += 1

        loop = AcceptanceLoop(
            AcceptanceChecker(), export_fn=lambda w: [path], heal_fn=heal
        )
        outcome = loop.run(session, TaskRequirements(), max_heal_rounds=max_rounds)

    assert heal_calls["n"] == 0  # 乱码不可自愈 → 不触发
    assert outcome.delivered is False
    assert ws.to_dict() == before  # 工作区未被破坏


# --------------------------------------------------------------------------- #
# Property 8: 章节写作有全局上下文
# --------------------------------------------------------------------------- #

@settings(max_examples=40)
@given(
    n_sections=st.integers(min_value=1, max_value=4),
    glossary_terms=st.lists(
        st.text(alphabet="abcdefghij", min_size=1, max_size=5), max_size=3, unique=True
    ),
)
def test_p8_curated_context_has_global_info(n_sections, glossary_terms):
    ws = PaperWorkspace(workspace_id="w", input_mode=InputMode.GENERATION)
    ws.outline = [
        OutlineNode(section_id=f"s{i}", title=f"Sec{i}", order=i)
        for i in range(n_sections)
    ]
    ws.glossary = {t: f"def-{t}" for t in glossary_terms}
    ws.section_drafts = {"s0": SectionDraft(section_id="s0", title="Sec0", content="正文")}

    ctx_text = build_curated_context(ws, "s0")
    # 全局大纲中的每个章节标题都在精选上下文里（全局理解，非孤立目标章节）。
    for node in ws.ordered_sections():
        assert node.title in ctx_text
    for term in glossary_terms:
        assert term in ctx_text


# --------------------------------------------------------------------------- #
# Property 9: 向后兼容（无可测约束时收尾器 no-op）
# --------------------------------------------------------------------------- #

def test_p9_no_requirements_noop_backward_compat(tmp_path):
    from paper_agent.agent_platform.finalize import make_acceptance_finalizer
    from paper_agent.agent_platform.task_agent import TaskAgent
    from paper_agent.providers.llm.base import LLMResponse
    from paper_agent.tools.registry import ToolRegistry

    class _LLM:
        def complete(self, messages, **opts):
            return LLMResponse(content="聊聊而已。")

    ws = _ws(1, ["1"])
    session = AgentSession(session_id="w", workspace=ws, task=WritingTask("随便聊聊"))
    before = copy.deepcopy(ws.to_dict())
    agent = TaskAgent(
        _LLM(), ToolRegistry(),
        acceptance_finalizer=make_acceptance_finalizer(str(tmp_path)),
    )
    result = agent.run(session)
    # 无可测约束（无格式关键词）→ 收尾 no-op：无验收产物、工作区不变。
    assert result.completed == [] and result.unfinished == []
    assert "acceptance_passed" not in result.guardrail_report
    assert ws.to_dict() == before


# --------------------------------------------------------------------------- #
# Property 6: 评审只读
# --------------------------------------------------------------------------- #

class _ReviewLLM:
    def complete(self, messages, **opts):
        from paper_agent.providers.llm.base import LLMResponse
        return LLMResponse(content=(
            '{"scores": {"logic": 7.0, "novelty": 6.0, "sufficiency": 6.5, '
            '"language": 8.0}}'
        ))


@settings(max_examples=20)
@given(st.text(alphabet="中文正abcXYZ 。，", min_size=1, max_size=60))
def test_p6_review_paper_read_only(content):
    from paper_agent.agent_platform.guardrail_gate import GuardrailGate
    from paper_agent.agent_platform.tools.context import ToolContext
    from paper_agent.agent_platform.tools.review import register_review_paper
    from paper_agent.tools.registry import ToolRegistry

    class _Store:
        def __init__(self):
            self._d = {}

        def load(self, wid):
            raw = self._d.get(wid)
            return PaperWorkspace.from_dict(raw) if raw else None

        def save(self, ws):
            self._d[ws.workspace_id] = copy.deepcopy(ws.to_dict())

    from paper_agent.workspace.repository import WorkspaceRepository

    ws = PaperWorkspace(workspace_id="w", input_mode=InputMode.DRAFT_REVISION)
    ws.outline = [OutlineNode(section_id="s1", title="Intro", order=0)]
    ws.section_drafts = {"s1": SectionDraft(section_id="s1", title="Intro", content=content)}
    repo = WorkspaceRepository(_Store())
    repo.create(ws)
    session = AgentSession(session_id="w", workspace=ws, task=WritingTask("评审"))
    ctx = ToolContext(session=session, repo=repo, gate=GuardrailGate(), elicitor=None)
    registry = ToolRegistry()
    register_review_paper(registry, ctx, _ReviewLLM())

    before = copy.deepcopy(ws.to_dict())
    registry.call("review_paper")
    assert ws.to_dict() == before
    assert ws.review_records == []


# --------------------------------------------------------------------------- #
# Property 7: 子智能体写入一致性（护栏拒绝的改动绝不落盘）
# --------------------------------------------------------------------------- #

@settings(max_examples=12, deadline=None)
@given(st.integers(min_value=100, max_value=999))
def test_p7_subagent_rejected_write_not_persisted(bad_id):
    from paper_agent.agent_platform.guardrail_gate import GuardrailGate
    from paper_agent.agent_platform.subagents import SubAgentRunner
    from paper_agent.agent_platform.tools.context import ToolContext
    from paper_agent.agent_platform.tools.edit import register_rewrite_section
    from paper_agent.elicitation import AutoElicitor
    from paper_agent.providers.llm.base import LLMResponse, ToolCall
    from paper_agent.tools.quality_gate import QualityGate
    from paper_agent.tools.registry import ToolRegistry
    from paper_agent.workspace.repository import WorkspaceRepository

    class _Store:
        def __init__(self):
            self._d = {}

        def load(self, wid):
            raw = self._d.get(wid)
            return PaperWorkspace.from_dict(raw) if raw else None

        def save(self, ws):
            self._d[ws.workspace_id] = copy.deepcopy(ws.to_dict())

    class _LLM:
        def __init__(self):
            self._i = 0

        def complete(self, messages, **opts):
            self._i += 1
            if self._i == 1:
                return LLMResponse(content="", tool_calls=[ToolCall(
                    id="c1", name="rewrite_section",
                    arguments={"section_id": "s1", "new_content": f"伪造 [{bad_id}]"},
                )])
            return LLMResponse(content="完成。")

    ws = PaperWorkspace(workspace_id="w1", input_mode=InputMode.DRAFT_REVISION)
    ws.outline = [OutlineNode(section_id="s1", title="Intro", order=0)]
    ws.section_drafts = {"s1": SectionDraft(section_id="s1", title="Intro", content="旧内容")}
    repo = WorkspaceRepository(_Store())
    repo.create(ws)
    session = AgentSession(session_id="w1", workspace=ws, task=WritingTask("t"))
    ctx = ToolContext(
        session=session, repo=repo,
        gate=GuardrailGate(quality_gate=QualityGate()), elicitor=AutoElicitor(),
    )
    registry = ToolRegistry()
    register_rewrite_section(registry, ctx)

    SubAgentRunner(_LLM()).run(session, "改写", registry=registry)
    assert ws.section_drafts["s1"].content == "旧内容"
