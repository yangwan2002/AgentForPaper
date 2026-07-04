"""上下文管理与图表处理测试（Req 5.2/5.3 / Req 6）。"""

from __future__ import annotations

from paper_agent.agents.base import AgentContext
from paper_agent.agents.writing_agent import WritingAgent
from paper_agent.context.manager import ContextManager, estimate_tokens
from paper_agent.providers.llm.mock import MockLLMProvider
from paper_agent.workspace.models import (
    FigureRecord,
    InputMode,
    OutlineNode,
    PaperWorkspace,
)


def test_context_manager_budget_trims_summaries():
    ws = PaperWorkspace(
        workspace_id="w", input_mode=InputMode.GENERATION, topic_background="x"
    )
    ws.outline = [
        OutlineNode(section_id=f"s{i}", title=f"S{i}", order=i) for i in range(5)
    ]
    ws.section_summaries = {f"s{i}": "摘要" * 50 for i in range(5)}

    cm = ContextManager(MockLLMProvider(), token_budget=30)
    block = cm.build_context(ws, current_section_id="s0")
    # 预算很小 → 摘要被裁剪，长度受限。
    assert estimate_tokens(block.summaries) <= 30 + 50


def test_user_caption_preserved_and_missing_generated():
    """Req 6.1：用户说明沿用；Req 6.2：缺失则生成。"""
    ws = PaperWorkspace(
        workspace_id="w", input_mode=InputMode.GENERATION, topic_background="x"
    )
    ws.outline = [OutlineNode(section_id="s0", title="S0", order=0)]
    ws.figures = [
        FigureRecord(
            figure_id="f1", data_ref="fig1.png",
            caption="用户提供的说明", caption_provided_by_user=True,
        ),
        FigureRecord(figure_id="f2", data_ref="fig2.png"),
    ]
    agent = WritingAgent(MockLLMProvider(), ContextManager(MockLLMProvider()))
    result = agent.run(AgentContext(workspace=ws))
    for mutation in result.mutations:
        mutation(ws)

    f1 = next(f for f in ws.figures if f.figure_id == "f1")
    f2 = next(f for f in ws.figures if f.figure_id == "f2")
    assert f1.caption == "用户提供的说明"   # 沿用
    assert f2.caption                          # 已生成，非空
