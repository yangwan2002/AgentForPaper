"""Prompt/JSON 解析与规划智能体 LLM 路径测试。"""

from __future__ import annotations

import json

from paper_agent.agents.base import AgentContext
from paper_agent.agents.plan_agent import PlanAgent
from paper_agent.providers.llm.mock import MockLLMProvider
from paper_agent.utils.json_parse import extract_json
from paper_agent.workspace.models import InputMode, PaperWorkspace


def test_extract_json_from_fenced_block():
    text = "好的，结果如下：\n```json\n{\"a\": 1}\n```\n完毕"
    assert extract_json(text) == {"a": 1}


def test_extract_json_plain_and_embedded():
    assert extract_json('{"x": [1, 2]}') == {"x": [1, 2]}
    assert extract_json("前缀 {\"y\": 3} 后缀") == {"y": 3}
    assert extract_json("没有 JSON") is None


def test_plan_agent_uses_llm_json_outline():
    """LLM 返回合法 JSON 大纲时，规划智能体据此生成大纲与检索标记。"""
    scripted = json.dumps(
        {
            "sections": [
                {"section_id": "intro", "title": "引言",
                 "summary_hint": "背景", "needs_retrieval": False},
                {"section_id": "rw", "title": "相关研究",
                 "summary_hint": "综述", "needs_retrieval": True},
            ]
        }
    )
    agent = PlanAgent(MockLLMProvider(scripted=[scripted]))
    ws = PaperWorkspace(
        workspace_id="w", input_mode=InputMode.GENERATION, topic_background="主题"
    )
    result = agent.run(AgentContext(workspace=ws))
    for m in result.mutations:
        m(ws)

    assert [n.title for n in ws.ordered_sections()] == ["引言", "相关研究"]
    rw_task = next(t for t in ws.task_checklist if t.section_ref == "rw")
    assert rw_task.needs_retrieval is True


def test_plan_agent_falls_back_when_llm_not_json():
    """LLM 输出非 JSON（如默认 mock 回显）时，回退到启发式默认大纲。"""
    agent = PlanAgent(MockLLMProvider())  # 默认回显，非 JSON
    ws = PaperWorkspace(
        workspace_id="w", input_mode=InputMode.GENERATION, topic_background="主题"
    )
    result = agent.run(AgentContext(workspace=ws))
    for m in result.mutations:
        m(ws)

    titles = [n.title for n in ws.ordered_sections()]
    assert "引言" in titles and "相关工作" in titles  # 默认骨架
