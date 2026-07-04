"""平台正确性属性测试（任务 14，设计 Property 1-10）。

用 hypothesis 生成随机意图序列 / 章节集 / 边界配置，断言平台的核心不变式成立。
"""

from __future__ import annotations

import copy

from hypothesis import given, settings
from hypothesis import strategies as st

from paper_agent.agent_platform.apply import apply_screened, commit
from paper_agent.agent_platform.bounds import budget_exceeded, deadline_exceeded
from paper_agent.agent_platform.guardrail_gate import GuardrailGate
from paper_agent.agent_platform.models import (
    CHANGE_CITATION,
    CHANGE_CONTENT,
    AgentSession,
    GateOutcome,
    ProposedChange,
    TaskAgentConfig,
    WritingTask,
)
from paper_agent.agent_platform.task_agent import TaskAgent
from paper_agent.providers.llm.base import LLMResponse, ToolCall
from paper_agent.tools.registry import ToolRegistry
from paper_agent.workspace.models import (
    InputMode,
    OutlineNode,
    PaperWorkspace,
    ReferenceEntry,
    SectionDraft,
)
from paper_agent.workspace.repository import WorkspaceRepository


# --- 测试基础设施 ------------------------------------------------------------

class _MemStore:
    def __init__(self):
        self._data = {}

    def load(self, wid):
        raw = self._data.get(wid)
        return PaperWorkspace.from_dict(raw) if raw else None

    def save(self, ws):
        self._data[ws.workspace_id] = copy.deepcopy(ws.to_dict())


def _make_ws(section_ids):
    ws = PaperWorkspace(workspace_id="w1", input_mode=InputMode.DRAFT_REVISION)
    ws.outline = [OutlineNode(section_id=s, title=s, order=i) for i, s in enumerate(section_ids)]
    ws.section_drafts = {
        s: SectionDraft(section_id=s, title=s, content=f"内容-{s}") for s in section_ids
    }
    return ws


class _QualityBlocking:
    """把指定章节判为高严重度（模拟护栏拒绝）。"""

    def __init__(self, bad_sections):
        self._bad = set(bad_sections)

    def check(self, ws):
        issues = [
            {"type": "placeholder", "severity": "high", "section_id": s, "message": "坏"}
            for s in self._bad
        ]

        class R:
            pass
        r = R()
        r.issues = issues
        return r


# --- Property 1：护栏不可绕过 -----------------------------------------------

@settings(max_examples=60, deadline=None)
@given(
    section_ids=st.lists(
        st.sampled_from(["a", "b", "c", "d"]), min_size=1, max_size=4, unique=True
    ),
    bad=st.sets(st.sampled_from(["a", "b", "c", "d"]), max_size=4),
)
def test_property_guardrail_not_bypassable(section_ids, bad):
    """落盘的内容改动 ⊆ 通过闸门的改动；被拒章节内容字节不变（Property 1）。"""
    ws = _make_ws(section_ids)
    repo = WorkspaceRepository(_MemStore())
    repo.create(ws)
    gate = GuardrailGate(quality_gate=_QualityBlocking(bad))

    original = {s: ws.section_drafts[s].content for s in section_ids}

    def _mut(sid):
        def _m(w):
            w.section_drafts[sid].content = f"新-{sid}"
        return _m

    changes = [
        ProposedChange(mutation=_mut(s), kind=CHANGE_CONTENT, section_id=s)
        for s in section_ids
    ]
    outcome = commit(repo, ws, gate, changes)

    reloaded = repo.load("w1")
    for s in section_ids:
        if s in bad:
            # 被拒 → 字节不变。
            assert reloaded.section_drafts[s].content == original[s]
        else:
            # 通过 → 已落盘。
            assert reloaded.section_drafts[s].content == f"新-{s}"


# --- Property 2/3：单一写路径 + 原子一致性 ----------------------------------

@settings(max_examples=40, deadline=None)
@given(fail_index=st.integers(min_value=0, max_value=3))
def test_property_atomic_all_or_nothing(fail_index):
    """一批意图中途失败 → 全回滚，无部分写入（Property 3）。"""
    ws = _make_ws(["a", "b", "c", "d"])
    repo = WorkspaceRepository(_MemStore())
    repo.create(ws)
    original = {s: ws.section_drafts[s].content for s in ws.section_drafts}

    muts = []
    for i, s in enumerate(["a", "b", "c", "d"]):
        if i == fail_index:
            def _boom(w):
                raise RuntimeError("boom")
            muts.append(_boom)
        else:
            def _mk(sid):
                def _m(w):
                    w.section_drafts[sid].content = f"改-{sid}"
                return _m
            muts.append(_mk(s))

    outcome = GateOutcome(passed=True, accepted_mutations=muts)
    try:
        apply_screened(repo, ws, outcome)
    except RuntimeError:
        pass
    reloaded = repo.load("w1")
    for s in original:
        assert reloaded.section_drafts[s].content == original[s]


# --- Property 4：引用真实性单调 ---------------------------------------------

@settings(max_examples=50, deadline=None)
@given(
    verifiable=st.lists(st.integers(min_value=0, max_value=9), min_size=0, max_size=6, unique=True),
    total=st.integers(min_value=1, max_value=6),
)
def test_property_citation_only_verifiable_land(verifiable, total):
    """增补引用后，工作区所有文献均可核验；不可核验者永不落盘（Property 4）。"""
    ws = _make_ws(["a"])
    repo = WorkspaceRepository(_MemStore())
    repo.create(ws)

    verifiable_ids = {f"src-{i}" for i in verifiable}

    class _V:
        def verify(self, entry):
            return entry.source_id in verifiable_ids

    gate = GuardrailGate(citation_verifier=_V())
    refs = [
        ReferenceEntry(id=f"r{i}", title=f"T{i}", authors=["A"], year=2024, source_id=f"src-{i}")
        for i in range(total)
    ]
    change = ProposedChange(mutation=lambda w: None, kind=CHANGE_CITATION, references=refs)
    commit(repo, ws, gate, [change])

    reloaded = repo.load("w1")
    landed_sources = {r.source_id for r in reloaded.verified_references}
    # 落盘的每条都可核验。
    assert landed_sources <= verifiable_ids
    # 且落盘的都标记 verified。
    assert all(r.verified for r in reloaded.verified_references)


# --- Property 5：有界终止 ----------------------------------------------------

@settings(max_examples=30, deadline=None)
@given(max_iters=st.integers(min_value=1, max_value=8))
def test_property_bounded_termination(max_iters):
    """任意 max_iters 下，永远发起工具调用的任务必在有限步内终止并标注 bound（Property 5）。"""
    registry = ToolRegistry()
    registry.register("noop", "t", lambda: "ok", {"type": "object", "properties": {}, "required": []})

    class _AlwaysTool:
        def complete(self, messages, **opts):
            if opts.get("tools"):
                return LLMResponse(content="", tool_calls=[ToolCall(id="c", name="noop", arguments={})])
            return LLMResponse(content="收尾")

    agent = TaskAgent(_AlwaysTool(), registry, config=TaskAgentConfig(max_iters=max_iters))
    ws = _make_ws(["a"])
    session = AgentSession(session_id="w1", workspace=ws, task=WritingTask("x"))
    result = agent.run(session)
    assert result.bound_hit == "max_iters"


# --- Property 6：局部任务隔离（内容工具只动目标章节） ------------------------

@settings(max_examples=50, deadline=None)
@given(
    section_ids=st.lists(st.sampled_from(["a", "b", "c"]), min_size=2, max_size=3, unique=True),
    target_idx=st.integers(min_value=0, max_value=2),
)
def test_property_section_scope_isolation(section_ids, target_idx):
    """改写单章节后，其余章节字节不变（Property 6）。"""
    target = section_ids[target_idx % len(section_ids)]
    ws = _make_ws(section_ids)
    repo = WorkspaceRepository(_MemStore())
    repo.create(ws)
    original = {s: ws.section_drafts[s].content for s in section_ids}

    def _m(w):
        w.section_drafts[target].content = "改了目标"

    commit(repo, ws, GuardrailGate(), [ProposedChange(mutation=_m, kind=CHANGE_CONTENT, section_id=target)])
    reloaded = repo.load("w1")
    for s in section_ids:
        if s == target:
            assert reloaded.section_drafts[s].content == "改了目标"
        else:
            assert reloaded.section_drafts[s].content == original[s]


# --- Property 5（bounds 纯函数） --------------------------------------------

@given(cap=st.integers(min_value=1, max_value=1000), used=st.integers(min_value=0, max_value=2000))
def test_property_budget_exceeded_monotone(cap, used):
    assert budget_exceeded(used, cap) == (used >= cap)


@given(limit=st.floats(min_value=-5, max_value=5))
def test_property_deadline_nonpositive_never_exceeds(limit):
    import time
    if limit <= 0:
        assert deadline_exceeded(time.monotonic() - 100, limit) is False
