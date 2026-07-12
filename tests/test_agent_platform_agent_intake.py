"""TaskAgent / TaskIntake / session_store 测试（任务 9/10/11）。

用可编排的 fake LLM（按脚本决定每轮是否发起工具调用）驱动顶层循环，验证：
- 多轮工具编排、自然收尾、有界终止（max_iters/deadline/token_budget）；
- 工具失败回灌不中止；
- 意图受理：空任务拒绝、Legacy 合成、续跑；
- 会话持久化与恢复。
"""

from __future__ import annotations

import copy

import pytest

from paper_agent.agent_platform.intake import TaskIntake
from paper_agent.agent_platform.models import (
    AgentSession,
    TaskAgentConfig,
    WritingTask,
)
from paper_agent.agent_platform.session_store import load_session, save_session
from paper_agent.agent_platform.task_agent import TaskAgent
from paper_agent.ingestion import IngestionConfirmationRequired
from paper_agent.observability.usage import UsageTracker
from paper_agent.orchestrator import InputValidationError
from paper_agent.providers.llm.base import LLMResponse, ToolCall
from paper_agent.tools.registry import ToolRegistry
from paper_agent.workspace.models import (
    InputMode,
    OutputFormat,
    PaperWorkspace,
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
    """按预置脚本逐轮返回 LLMResponse；脚本耗尽后返回无工具调用的收尾答复。"""

    def __init__(self, script):
        self._script = list(script)
        self._i = 0

    def complete(self, messages, **opts):
        if self._i < len(self._script):
            resp = self._script[self._i]
            self._i += 1
            return resp
        return LLMResponse(content="任务完成收尾。")


def _session(instruction="做点什么"):
    ws = PaperWorkspace(workspace_id="w1", input_mode=InputMode.DRAFT_REVISION)
    return AgentSession(session_id="w1", workspace=ws, task=WritingTask(instruction))


# --- TaskAgent 循环 ----------------------------------------------------------

def test_agent_natural_finish_no_tools():
    llm = _ScriptedLLM([LLMResponse(content="无需工具，直接答复。")])
    agent = TaskAgent(llm, ToolRegistry())
    result = agent.run(_session())
    assert result.summary == "无需工具，直接答复。"
    assert result.bound_hit is None


def test_agent_multi_tool_then_finish():
    calls = []
    registry = ToolRegistry()
    registry.register("noop", "测试工具", lambda x=None: f"ok:{x}", {
        "type": "object", "properties": {"x": {"type": "string"}}, "required": []})

    script = [
        LLMResponse(content="", tool_calls=[ToolCall(id="c1", name="noop", arguments={"x": "1"})]),
        LLMResponse(content="", tool_calls=[ToolCall(id="c2", name="noop", arguments={"x": "2"})]),
        LLMResponse(content="两步完成。"),
    ]
    agent = TaskAgent(_ScriptedLLM(script), registry)
    session = _session()
    result = agent.run(session)
    assert result.summary == "两步完成。"
    # transcript 记录了两次工具调用。
    tool_calls = [e for e in session.transcript if e.get("kind") == "tool_call"]
    assert len(tool_calls) == 2


def test_agent_tool_failure_is_fed_back_not_raised():
    registry = ToolRegistry()

    def _boom():
        raise RuntimeError("工具炸了")

    registry.register("boom", "会失败", _boom, {"type": "object", "properties": {}, "required": []})
    script = [
        LLMResponse(content="", tool_calls=[ToolCall(id="c1", name="boom", arguments={})]),
        LLMResponse(content="已处理失败并收尾。"),
    ]
    agent = TaskAgent(_ScriptedLLM(script), registry)
    result = agent.run(_session())
    assert result.summary == "已处理失败并收尾。"


def test_agent_max_iters_bound():
    # 每轮都发起工具调用，永不自然收尾 → 触达 max_iters。
    registry = ToolRegistry()
    registry.register("noop", "t", lambda: "ok", {"type": "object", "properties": {}, "required": []})

    class _AlwaysTool:
        def complete(self, messages, **opts):
            if any(m.role == "tool" for m in messages[-1:]) or True:
                # 有 tools 参数时发起调用；无 tools（强制收尾）时返回收尾。
                if opts.get("tools"):
                    return LLMResponse(content="", tool_calls=[ToolCall(id="c", name="noop", arguments={})])
                return LLMResponse(content="被迫收尾。")

    agent = TaskAgent(_AlwaysTool(), registry, config=TaskAgentConfig(max_iters=3))
    result = agent.run(_session())
    assert result.bound_hit == "max_iters"
    assert result.summary == "被迫收尾。"


def test_agent_token_budget_bound():
    tracker = UsageTracker()
    tracker.prompt_tokens = 10_000  # 已超预算
    registry = ToolRegistry()
    agent = TaskAgent(
        _ScriptedLLM([LLMResponse(content="不该走到这")]),
        registry,
        tracker=tracker,
        token_budget=100,
    )
    result = agent.run(_session())
    assert result.bound_hit == "token_budget"


def test_agent_deadline_bound():
    import time

    registry = ToolRegistry()
    # 工具 sleep 5ms；deadline=1ms → 第二轮开头的检查必然已超时。
    registry.register(
        "slow", "慢工具", lambda: (time.sleep(0.005) or "ok"),
        {"type": "object", "properties": {}, "required": []},
    )

    class _AlwaysTool:
        def complete(self, messages, **opts):
            if opts.get("tools"):
                return LLMResponse(content="", tool_calls=[ToolCall(id="c", name="slow", arguments={})])
            return LLMResponse(content="超时收尾。")

    agent = TaskAgent(_AlwaysTool(), registry, deadline_s=0.001)
    result = agent.run(_session())
    assert result.bound_hit == "deadline"


# --- TaskIntake --------------------------------------------------------------

def _repo():
    return WorkspaceRepository(_MemStore())


def test_intake_rejects_empty_task_without_context():
    intake = TaskIntake(_repo())
    with pytest.raises(InputValidationError):
        intake.start(WritingTask(instruction="   "))


def test_intake_creates_workspace_from_topic():
    intake = TaskIntake(_repo())
    session = intake.start(WritingTask(instruction="写篇关于图神经网络的论文", topic_background="图神经网络"))
    assert session.workspace.input_mode is InputMode.GENERATION
    assert session.workspace.topic_background == "图神经网络"


def test_intake_legacy_synthesizes_draft_instruction():
    intake = TaskIntake(_repo(), draft_loader=lambda p: "初稿正文")
    # 无 instruction，但给了 draft_path → 合成修订润色任务。
    session = intake.start(WritingTask(instruction="", draft_path="paper.md"))
    assert session.task.instruction  # 已合成非空
    assert "润色" in session.task.instruction
    assert session.workspace.input_mode is InputMode.DRAFT_REVISION
    assert session.workspace.original_draft == "初稿正文"


def test_intake_legacy_synthesizes_topic_instruction():
    intake = TaskIntake(_repo())
    session = intake.start(WritingTask(instruction="", topic_background="量子计算"))
    assert "撰写" in session.task.instruction


def test_intake_draft_output_format_from_extension():
    intake = TaskIntake(_repo(), draft_loader=lambda p: "x")
    session = intake.start(WritingTask(instruction="改一下", draft_path="paper.tex"))
    assert session.workspace.output_format is OutputFormat.LATEX


def test_intake_persists_ingestion_quality_before_agent_runs(tmp_path):
    draft = tmp_path / "paper.md"
    draft.write_text("# Introduction\nReadable body with one \ufffd marker.", encoding="utf-8")
    intake = TaskIntake(_repo())

    session = intake.start(WritingTask(instruction="润色", draft_path=str(draft)))

    quality = session.workspace.profile["ingestion_quality"]
    assert quality["severity"] == "warning"
    assert quality["metrics"]["replacement_char_count"] == 1


def test_intake_surfaces_recoverable_confirmation_requirement(tmp_path):
    draft = tmp_path / "paper.md"
    draft.write_text("Readable academic prose. " * 220, encoding="utf-8")
    intake = TaskIntake(_repo())

    with pytest.raises(IngestionConfirmationRequired) as raised:
        intake.start(WritingTask(instruction="润色", draft_path=str(draft)))

    assert raised.value.report.status == "confirmation_required"
    session = intake.start(
        WritingTask(
            instruction="润色",
            draft_path=str(draft),
            confirm_ingestion=True,
        )
    )
    assert session.workspace.profile["ingestion_quality"]["status"] == (
        "confirmation_required"
    )


# --- session_store 续跑 ------------------------------------------------------

def test_session_save_and_resume_roundtrip():
    repo = _repo()
    intake = TaskIntake(repo)
    session = intake.start(WritingTask(instruction="任务A", topic_background="主题"))
    session.record("tool_call", name="noop")
    save_session(repo, session)

    resumed = intake.resume(session.session_id)
    assert resumed.session_id == session.session_id
    assert resumed.task.instruction == "任务A"
    assert any(e.get("name") == "noop" for e in resumed.transcript)


def test_resume_missing_session_raises():
    intake = TaskIntake(_repo())
    with pytest.raises(InputValidationError):
        intake.resume("nonexistent")
