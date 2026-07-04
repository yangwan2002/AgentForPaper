"""PaperAgentApp 装配与端到端（Mock）测试（任务 12）。

用注入的 fake 依赖验证 app 的会话编排、工具注册与持久化；另用 build_agent_app +
mock provider 做一次装配 smoke test。
"""

from __future__ import annotations

import copy

import pytest

from paper_agent.agent_platform.app import (
    PaperAgentApp,
    build_agent_app,
    validate_agent_config,
)
from paper_agent.agent_platform.guardrail_gate import GuardrailGate
from paper_agent.agent_platform.models import TaskAgentConfig, WritingTask
from paper_agent.agent_platform.session_store import load_session
from paper_agent.config import Config
from paper_agent.providers.llm.base import LLMResponse, ToolCall
from paper_agent.workspace.models import (
    InputMode,
    OutputFormat,
    PaperWorkspace,
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


class _FakeRetrieval:
    def search(self, query, limit=5):
        return []

    def fetch_metadata(self, source_id):
        return None


# --- validate_agent_config ---------------------------------------------------

def test_validate_agent_config_ok():
    validate_agent_config(TaskAgentConfig())  # 默认值合法


def test_validate_agent_config_rejects_out_of_range():
    with pytest.raises(ValueError):
        validate_agent_config(TaskAgentConfig(max_iters=0))
    with pytest.raises(ValueError):
        validate_agent_config(TaskAgentConfig(keep_recent_turns=999))


# --- PaperAgentApp（注入 fake） ---------------------------------------------

def _app(llm, repo=None):
    from paper_agent.tools.citation import CitationVerifier

    retrieval = _FakeRetrieval()
    repo = repo or WorkspaceRepository(_MemStore())
    return PaperAgentApp(
        llm=llm,
        repo=repo,
        gate=GuardrailGate(),
        retrieval=retrieval,
        verifier=CitationVerifier(retrieval),
        pipeline_runner=lambda wid: None,
        output_dir="output",
    )


def test_app_run_task_executes_rewrite_and_persists(tmp_path):
    # 预置一个带章节的工作区，任务让 agent 改写它。
    store = _MemStore()
    repo = WorkspaceRepository(store)
    ws = PaperWorkspace(workspace_id="w1", input_mode=InputMode.DRAFT_REVISION)
    ws.section_drafts = {"intro": SectionDraft(section_id="intro", title="引言", content="旧引言")}
    ws.outline = []
    repo.create(ws)

    script = [
        LLMResponse(content="", tool_calls=[
            ToolCall(id="c1", name="rewrite_section",
                     arguments={"section_id": "intro", "new_content": "崭新的引言叙述。"})
        ]),
        LLMResponse(content="已改写引言。"),
    ]
    app = _app(_ScriptedLLM(script), repo=repo)
    result = app.run_task(WritingTask(instruction="改写引言", workspace_id="w1"))

    assert "已改写引言" in result.summary
    assert repo.load("w1").section_drafts["intro"].content == "崭新的引言叙述。"
    # 会话已持久化，可恢复。
    resumed = load_session(repo, "w1")
    assert resumed is not None
    assert resumed.task.instruction == "改写引言"


def test_app_rejects_empty_task():
    from paper_agent.orchestrator import InputValidationError

    app = _app(_ScriptedLLM([]))
    with pytest.raises(InputValidationError):
        app.run_task(WritingTask(instruction="  "))


# --- build_agent_app（mock provider 装配 smoke） -----------------------------

def test_build_agent_app_mock_smoke(tmp_path):
    config = Config(
        llm_provider="mock",
        retrieval_provider="mock",
        workspace_dir=str(tmp_path),
        default_output_format=OutputFormat.MARKDOWN,
    )
    app = build_agent_app(config, store=_MemStore())
    # mock LLM 通常不发起工具调用 → 单轮自然收尾；此处只验证可构造并跑通。
    result = app.run_task(WritingTask(instruction="随便写点什么", topic_background="测试主题"))
    assert result.session_id
    assert result.bound_hit is None
