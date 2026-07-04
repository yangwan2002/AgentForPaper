"""端到端集成与向后兼容测试（任务 15）。

用 Mock LLM + ScriptedElicitor 跑典型任务链路，并验证 Legacy 入口（无 instruction）
仍能受理初稿/主题。
"""

from __future__ import annotations

import copy

from paper_agent.agent_platform.app import PaperAgentApp
from paper_agent.agent_platform.guardrail_gate import GuardrailGate
from paper_agent.agent_platform.models import WritingTask
from paper_agent.providers.llm.base import LLMResponse, ToolCall
from paper_agent.tools.citation import CitationVerifier
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


class _ScriptedLLM:
    def __init__(self, script):
        self._script = list(script)
        self._i = 0

    def complete(self, messages, **opts):
        if self._i < len(self._script):
            r = self._script[self._i]
            self._i += 1
            return r
        return LLMResponse(content="完成。")


class _RetrievalWithRefs:
    """检索返回若干候选；fetch_metadata 判定 real- 前缀为真实。"""

    def __init__(self, refs):
        self._refs = refs

    def search(self, query, limit=5):
        return list(self._refs[:limit])

    def fetch_metadata(self, source_id):
        if source_id.startswith("real-"):
            return ReferenceEntry(id="x", title="t", authors=["a"], year=2024, source_id=source_id)
        return None


def _app(llm, repo, retrieval, pipeline_runner=lambda wid: None):
    verifier = CitationVerifier(retrieval)
    return PaperAgentApp(
        llm=llm,
        repo=repo,
        gate=GuardrailGate(quality_gate=None, citation_verifier=verifier),
        retrieval=retrieval,
        verifier=verifier,
        pipeline_runner=pipeline_runner,
        output_dir="output",
    )


def _seed_ws(repo, wid="w1"):
    ws = PaperWorkspace(workspace_id=wid, input_mode=InputMode.DRAFT_REVISION)
    ws.outline = [
        OutlineNode(section_id="intro", title="引言", order=0),
        OutlineNode(section_id="experiments", title="实验", order=1),
    ]
    ws.section_drafts = {
        "intro": SectionDraft(section_id="intro", title="引言", content="原始引言。"),
        "experiments": SectionDraft(section_id="experiments", title="实验", content="原始实验叙述。"),
    }
    repo.create(ws)
    return ws


# --- 端到端：定位 + 改写实验章节 --------------------------------------------

def test_e2e_locate_then_rewrite_experiments():
    repo = WorkspaceRepository(_MemStore())
    _seed_ws(repo)
    script = [
        LLMResponse(content="", tool_calls=[
            ToolCall(id="c1", name="locate_section", arguments={"reference": "实验"})]),
        LLMResponse(content="", tool_calls=[
            ToolCall(id="c2", name="read_section", arguments={"section_id": "experiments"})]),
        LLMResponse(content="", tool_calls=[
            ToolCall(id="c3", name="rewrite_section",
                     arguments={"section_id": "experiments", "new_content": "更简洁的实验叙述。"})]),
        LLMResponse(content="已改写实验章节的叙述方式。"),
    ]
    app = _app(_ScriptedLLM(script), repo, _RetrievalWithRefs([]))
    result = app.run_task(WritingTask(instruction="把实验章节叙述改简洁", workspace_id="w1"))
    assert "已改写实验章节" in result.summary
    assert repo.load("w1").section_drafts["experiments"].content == "更简洁的实验叙述。"
    # 引言未被动。
    assert repo.load("w1").section_drafts["intro"].content == "原始引言。"


# --- 端到端：增补文献（含不可核验的差额） -----------------------------------

def test_e2e_add_references_with_shortfall():
    repo = WorkspaceRepository(_MemStore())
    _seed_ws(repo)
    refs = [
        ReferenceEntry(id="r1", title="Real 1", authors=["A"], year=2024, source_id="real-1"),
        ReferenceEntry(id="r2", title="Fake 2", authors=["B"], year=2024, source_id="fake-2"),
    ]
    script = [
        LLMResponse(content="", tool_calls=[
            ToolCall(id="c1", name="add_references", arguments={"query": "topic", "limit": 5})]),
        LLMResponse(content="已尽力增补可核验文献。"),
    ]
    app = _app(_ScriptedLLM(script), repo, _RetrievalWithRefs(refs))
    result = app.run_task(WritingTask(instruction="加几篇相关文献", workspace_id="w1"))
    landed = repo.load("w1").verified_references
    titles = {r.title for r in landed}
    # 只有可核验的 real-1 落盘（重新编号为数字 id），伪造的不入库。
    assert "Real 1" in titles and "Fake 2" not in titles
    assert all(r.id.isdigit() for r in landed)  # 均为数字编号，正文 [n] 可对上


# --- 端到端：run_full_pipeline 复合工具 -------------------------------------

def test_e2e_run_full_pipeline_tool():
    repo = WorkspaceRepository(_MemStore())
    _seed_ws(repo)

    class _Result:
        terminated_reason = "quality_met"
        submittable = True
        class export:
            files = ["output/w1.md"]

    def _runner(wid):
        ws = repo.load(wid)
        ws.section_drafts["intro"].content = "管线产出的引言。"
        repo.update(ws, lambda w: w.section_drafts["intro"].__setattr__("content", "管线产出的引言。"))
        return _Result()

    script = [
        LLMResponse(content="", tool_calls=[
            ToolCall(id="c1", name="run_full_pipeline", arguments={})]),
        LLMResponse(content="已运行完整管线。"),
    ]
    app = _app(_ScriptedLLM(script), repo, _RetrievalWithRefs([]), pipeline_runner=_runner)
    result = app.run_task(WritingTask(instruction="从头把整篇写好", workspace_id="w1"))
    assert "已运行完整管线" in result.summary
    assert repo.load("w1").section_drafts["intro"].content == "管线产出的引言。"


# --- 向后兼容：Legacy 入口（无 instruction 合成任务） -----------------------

def test_backward_compat_legacy_topic_synthesizes_task():
    repo = WorkspaceRepository(_MemStore())
    app = _app(_ScriptedLLM([LLMResponse(content="已按主题起草。")]), repo, _RetrievalWithRefs([]))
    # 无 instruction，仅给主题 → 合成默认任务并受理。
    result = app.run_task(WritingTask(instruction="", topic_background="图神经网络综述"))
    assert result.session_id
    # 工作区以 GENERATION 模式创建。
    assert repo.load(result.session_id).input_mode is InputMode.GENERATION


def test_backward_compat_legacy_draft_synthesizes_task():
    repo = WorkspaceRepository(_MemStore())
    app = _app(_ScriptedLLM([LLMResponse(content="已修订润色。")]), repo, _RetrievalWithRefs([]))
    app_intake_task = WritingTask(instruction="", draft_path=None, topic_background="X")
    result = app.run_task(app_intake_task)
    assert result.session_id
