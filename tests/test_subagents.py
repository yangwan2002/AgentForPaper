"""选择性子智能体与并行原语测试（Task 6）。

验证：并行任务隔离（单个失败不影响其余）、并行文献核验聚合、子智能体写入经护栏
与单一写路径、章节写作精选上下文含全局信息（大纲/术语表/相邻摘要/目标全文）。
"""

from __future__ import annotations

import copy

from paper_agent.agent_platform.guardrail_gate import GuardrailGate
from paper_agent.agent_platform.models import AgentSession, WritingTask
from paper_agent.agent_platform.subagents import (
    ParallelResult,
    SubAgentRunner,
    build_curated_context,
    run_parallel,
    verify_references_parallel,
)
from paper_agent.agent_platform.tools.context import ToolContext
from paper_agent.agent_platform.tools.edit import register_rewrite_section
from paper_agent.elicitation import AutoElicitor
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


class _MemStore:
    def __init__(self):
        self._data = {}

    def load(self, wid):
        raw = self._data.get(wid)
        return PaperWorkspace.from_dict(raw) if raw else None

    def save(self, ws):
        self._data[ws.workspace_id] = copy.deepcopy(ws.to_dict())


# --------------------------------------------------------------------------- #
# run_parallel
# --------------------------------------------------------------------------- #

def test_run_parallel_preserves_order_and_isolates_failures():
    def ok(n):
        return lambda: n * 2

    def boom():
        raise ValueError("boom")

    tasks = [ok(1), boom, ok(3)]
    results = run_parallel(tasks, max_workers=3)
    assert len(results) == 3
    assert results[0] == ParallelResult(ok=True, value=2)
    assert results[1].ok is False and "boom" in results[1].error
    assert results[2] == ParallelResult(ok=True, value=6)


def test_run_parallel_empty():
    assert run_parallel([]) == []


# --------------------------------------------------------------------------- #
# verify_references_parallel
# --------------------------------------------------------------------------- #

class _FakeVerifier:
    """按 source_id 前缀决定是否核验通过。"""

    def verify_and_mark(self, entry: ReferenceEntry) -> ReferenceEntry:
        data = vars(entry).copy()
        data["verified"] = entry.source_id.startswith("real")
        return ReferenceEntry(**data)


def test_verify_references_parallel_marks_each():
    entries = [
        ReferenceEntry(id="1", title="A", authors=["X"], year=2020, source_id="real:1"),
        ReferenceEntry(id="2", title="B", authors=["Y"], year=2021, source_id="fake:2"),
    ]
    marked = verify_references_parallel(_FakeVerifier(), entries, max_workers=2)
    assert [m.verified for m in marked] == [True, False]
    # 原条目未被原地修改（返回新对象）。
    assert entries[0].verified is False


# --------------------------------------------------------------------------- #
# build_curated_context
# --------------------------------------------------------------------------- #

def _ws_multi() -> PaperWorkspace:
    ws = PaperWorkspace(workspace_id="w", input_mode=InputMode.GENERATION)
    ws.outline = [
        OutlineNode(section_id="intro", title="引言", order=0),
        OutlineNode(section_id="method", title="方法", order=1),
        OutlineNode(section_id="exp", title="实验", order=2),
    ]
    ws.glossary = {"CNN": "卷积神经网络"}
    ws.section_summaries = {"intro": "背景与动机", "exp": "实验结果概述"}
    ws.section_drafts = {
        "method": SectionDraft(section_id="method", title="方法", content="方法章节全文内容。"),
    }
    return ws


def test_build_curated_context_contains_global_info():
    ws = _ws_multi()
    ctx_text = build_curated_context(ws, "method")
    # 全局大纲 + 术语表（全局理解）
    assert "引言" in ctx_text and "方法" in ctx_text and "实验" in ctx_text
    assert "CNN" in ctx_text and "卷积神经网络" in ctx_text
    # 相邻章节摘要
    assert "背景与动机" in ctx_text or "实验结果概述" in ctx_text
    # 目标章节全文
    assert "方法章节全文内容。" in ctx_text


# --------------------------------------------------------------------------- #
# SubAgentRunner：写入经护栏 + 单一写路径
# --------------------------------------------------------------------------- #

class _ScriptedLLM:
    def __init__(self, script):
        self._script = list(script)
        self._i = 0

    def complete(self, messages, **opts):
        if self._i < len(self._script):
            resp = self._script[self._i]
            self._i += 1
            return resp
        return LLMResponse(content="子任务完成。")


def test_subagent_writes_through_guardrail_and_shared_workspace():
    ws = PaperWorkspace(workspace_id="w1", input_mode=InputMode.DRAFT_REVISION)
    ws.outline = [OutlineNode(section_id="s1", title="Intro", order=0)]
    ws.verified_references = [
        ReferenceEntry(id="1", title="A", authors=["X"], year=2020, source_id="d1", verified=True)
    ]
    ws.section_drafts = {"s1": SectionDraft(section_id="s1", title="Intro", content="旧内容 [1]")}
    repo = WorkspaceRepository(_MemStore())
    repo.create(ws)
    session = AgentSession(session_id="w1", workspace=ws, task=WritingTask("子任务"))

    ctx = ToolContext(session=session, repo=repo, gate=GuardrailGate(), elicitor=AutoElicitor())
    registry = ToolRegistry()
    register_rewrite_section(registry, ctx)

    new_content = "新内容 [1]"
    script = [
        LLMResponse(content="", tool_calls=[ToolCall(
            id="c1", name="rewrite_section",
            arguments={"section_id": "s1", "new_content": new_content},
        )]),
        LLMResponse(content="已改写。"),
    ]
    runner = SubAgentRunner(_ScriptedLLM(script))
    result = runner.run(
        session, "把 s1 改写为新内容",
        registry=registry,
        curated_context=build_curated_context(ws, "s1"),
    )
    assert result.delivered is True
    # 子智能体经既有写工具 → 护栏 → 单一写路径改了共享工作区。
    assert ws.section_drafts["s1"].content == new_content


def test_subagent_rejected_write_not_persisted():
    """子智能体尝试引用未核验文献 → 护栏拒绝 → 工作区不变。"""
    ws = PaperWorkspace(workspace_id="w1", input_mode=InputMode.DRAFT_REVISION)
    ws.outline = [OutlineNode(section_id="s1", title="Intro", order=0)]
    ws.section_drafts = {"s1": SectionDraft(section_id="s1", title="Intro", content="旧内容")}
    repo = WorkspaceRepository(_MemStore())
    repo.create(ws)
    session = AgentSession(session_id="w1", workspace=ws, task=WritingTask("子任务"))

    from paper_agent.tools.quality_gate import QualityGate

    ctx = ToolContext(
        session=session,
        repo=repo,
        gate=GuardrailGate(quality_gate=QualityGate()),
        elicitor=AutoElicitor(),
    )
    registry = ToolRegistry()
    register_rewrite_section(registry, ctx)

    script = [
        LLMResponse(content="", tool_calls=[ToolCall(
            id="c1", name="rewrite_section",
            arguments={"section_id": "s1", "new_content": "伪造引用 [999]"},
        )]),
        LLMResponse(content="尝试完成。"),
    ]
    runner = SubAgentRunner(_ScriptedLLM(script))
    runner.run(session, "改写", registry=registry)
    # 护栏拒绝未核验引用 → 内容保持旧值（单一写路径 all-or-nothing）。
    assert ws.section_drafts["s1"].content == "旧内容"
