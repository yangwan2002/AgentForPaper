"""断点续跑测试：从已保存的工作区继续，跳过已完成阶段、补齐缺失章节。"""

from __future__ import annotations

from paper_agent.agents.base import AgentContext
from paper_agent.agents.writing_agent import WritingAgent
from paper_agent.app import build_from_config
from paper_agent.config import Config
from paper_agent.context.manager import ContextManager
from paper_agent.orchestrator import InputValidationError
from paper_agent.providers.llm.mock import MockLLMProvider
from paper_agent.workspace.models import (
    InputMode,
    OutlineNode,
    PaperWorkspace,
    SectionDraft,
)
from paper_agent.workspace.repository import WorkspaceRepository
from paper_agent.workspace.store import InMemoryStore


def test_writing_agent_fills_only_missing_sections():
    """已有部分章节草稿时，仅补齐缺失章节，不动已写章节。"""
    ws = PaperWorkspace(
        workspace_id="w", input_mode=InputMode.GENERATION, topic_background="x"
    )
    ws.outline = [
        OutlineNode(section_id="a", title="A", order=0),
        OutlineNode(section_id="b", title="B", order=1),
    ]
    ws.section_drafts = {
        "a": SectionDraft(section_id="a", title="A", content="已写A")
    }
    agent = WritingAgent(MockLLMProvider(), ContextManager(MockLLMProvider()))
    result = agent.run(AgentContext(workspace=ws))
    for m in result.mutations:
        m(ws)

    assert ws.section_drafts["a"].content == "已写A"     # 未动
    assert "b" in ws.section_drafts                        # 补齐
    assert ws.section_drafts["b"].content                  # 非空


def test_resume_unknown_workspace_raises(tmp_path):
    orch = build_from_config(
        Config(workspace_dir=str(tmp_path)), store=InMemoryStore()
    )
    import pytest

    with pytest.raises(InputValidationError):
        orch.run(resume_id="does_not_exist")


def test_resume_completes_partial_workspace(tmp_path):
    """先保存一个"已规划但未写完"的工作区，再续跑直到完成。"""
    store = InMemoryStore()
    repo = WorkspaceRepository(store)
    ws = PaperWorkspace(
        workspace_id="resume1", input_mode=InputMode.GENERATION,
        topic_background="多智能体写作",
    )
    ws.outline = [
        OutlineNode(section_id="intro", title="引言", order=0),
        OutlineNode(section_id="concl", title="结论", order=1),
    ]
    repo.create(ws)  # 已规划，无草稿

    orch = build_from_config(
        Config(quality_threshold=8.0, iteration_limit=3, workspace_dir=str(tmp_path)),
        store=store,
    )
    result = orch.run(resume_id="resume1")

    reloaded = repo.load("resume1")
    # 章节被补齐。
    assert "intro" in reloaded.section_drafts
    assert "concl" in reloaded.section_drafts
    # Mock provider 的评审为 MOCK_FALLBACK（不可信），续跑在迭代上限内以可诊断原因终止。
    assert result.terminated_reason in {
        "quality_met",
        "iteration_limit",
        "iteration_limit_unparsed_review",
    }
