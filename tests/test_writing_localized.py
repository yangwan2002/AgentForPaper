"""写作智能体局部修改测试（Property 5 / Req 5.7-5.9）。"""

from __future__ import annotations

from paper_agent.agents.base import AgentContext
from paper_agent.agents.writing_agent import WritingAgent
from paper_agent.context.manager import ContextManager
from paper_agent.providers.llm.base import ToolCall
from paper_agent.providers.llm.mock import MockLLMProvider
from paper_agent.providers.retrieval.base import RetrievalProvider
from paper_agent.tools.citation import CitationVerifier
from paper_agent.workspace.models import (
    InputMode,
    OutlineNode,
    PaperWorkspace,
    SectionDraft,
    SectionEdit,
)


class _EmptyRetrieval(RetrievalProvider):
    """最小检索桩：用于启用写作智能体的工具模式（不返回任何候选）。"""

    def search(self, query, limit=10):
        return []

    def fetch_metadata(self, identifier):
        return None


def _ws_with_drafts() -> PaperWorkspace:
    ws = PaperWorkspace(
        workspace_id="w", input_mode=InputMode.GENERATION, topic_background="x"
    )
    ws.outline = [
        OutlineNode(section_id="a", title="A", order=0),
        OutlineNode(section_id="b", title="B", order=1),
    ]
    ws.section_drafts = {
        "a": SectionDraft(section_id="a", title="A", content="原始A内容"),
        "b": SectionDraft(section_id="b", title="B", content="原始B内容"),
    }
    return ws


def test_localized_edit_only_changes_targeted_section():
    """Property 5：仅目标章节被修改，其余章节字节级保持不变。"""
    ws = _ws_with_drafts()
    agent = WritingAgent(MockLLMProvider(), ContextManager(MockLLMProvider()))

    result = agent.run(
        AgentContext(workspace=ws, extras={"edits": {"a": "改进逻辑"}})
    )
    for mutation in result.mutations:
        mutation(ws)

    assert ws.section_drafts["a"].content != "原始A内容"  # 目标章节已改
    assert ws.section_drafts["b"].content == "原始B内容"  # 未涉及章节不变


def test_no_suggestion_no_change():
    ws = _ws_with_drafts()
    agent = WritingAgent(MockLLMProvider(), ContextManager(MockLLMProvider()))
    result = agent.run(AgentContext(workspace=ws, extras={}))
    for mutation in result.mutations:
        mutation(ws)
    assert ws.section_drafts["a"].content == "原始A内容"
    assert ws.section_drafts["b"].content == "原始B内容"


def test_structural_remove_section():
    ws = _ws_with_drafts()
    agent = WritingAgent(MockLLMProvider(), ContextManager(MockLLMProvider()))
    result = agent.run(
        AgentContext(workspace=ws, extras={"structural": {"remove": ["b"]}})
    )
    for mutation in result.mutations:
        mutation(ws)
    assert "b" not in ws.section_drafts
    assert "a" in ws.section_drafts  # 未受影响章节保留


def test_extract_cited_selects_only_used_refs():
    """_extract_cited 只挑正文实际出现的文献 id，不堆砌全部。"""
    agent = WritingAgent(MockLLMProvider(), ContextManager(MockLLMProvider()))
    content = "本节方法参考了 [arxiv:1706.03762]，但未用到其他文献。"
    available = ["arxiv:1706.03762", "openalex:W999", "arxiv:2301.10140"]
    cited = agent._extract_cited(content, available)
    assert cited == ["arxiv:1706.03762"]


# --------------------------------------------------------------------------- #
# 工具模式局部修订：汇聚 SectionEdit → WorkspaceMutation（Req 6.9 / 9.1 / 9.3，Property 9）
# --------------------------------------------------------------------------- #


def _tool_mode_agent(llm: MockLLMProvider) -> WritingAgent:
    provider = _EmptyRetrieval()
    return WritingAgent(
        llm,
        ContextManager(MockLLMProvider()),
        retrieval=provider,
        verifier=CitationVerifier(provider),
    )


def test_tool_mode_localized_revision_aggregates_edit_section():
    """模型经 edit_section 产出意图，WritingAgent 汇聚后仅改目标章节。"""
    ws = _ws_with_drafts()
    # 第 1 回合：请求 edit_section（替换 a 的内容）；第 2 回合：给出收尾文本。
    llm = MockLLMProvider(
        scripted=[
            [
                ToolCall(
                    id="c1",
                    name="edit_section",
                    arguments={
                        "section_id": "a",
                        "anchor": "原始A内容",
                        "replacement": "精确替换后的A内容",
                        "mode": "replace",
                    },
                )
            ],
            "已完成本章修订。",
        ]
    )
    agent = _tool_mode_agent(llm)

    result = agent.run(
        AgentContext(workspace=ws, extras={"edits": {"a": "请改进A的逻辑"}})
    )
    for mutation in result.mutations:
        mutation(ws)

    # 目标章节按锚点精确替换；未涉及章节字节级不变（Property 9）。
    assert ws.section_drafts["a"].content == "精确替换后的A内容"
    assert ws.section_drafts["b"].content == "原始B内容"


def test_tool_mode_localized_revision_anchor_miss_leaves_section_unchanged():
    """锚点未命中时不产生工作区变更（Property 9 / Req 6.5）。"""
    ws = _ws_with_drafts()
    llm = MockLLMProvider(
        scripted=[
            [
                ToolCall(
                    id="c1",
                    name="edit_section",
                    arguments={
                        "section_id": "a",
                        "anchor": "不存在的锚点",
                        "replacement": "x",
                        "mode": "replace",
                    },
                )
            ],
            "完成。",
        ]
    )
    agent = _tool_mode_agent(llm)

    result = agent.run(
        AgentContext(workspace=ws, extras={"edits": {"a": "请改进A"}})
    )
    for mutation in result.mutations:
        mutation(ws)

    assert ws.section_drafts["a"].content == "原始A内容"
    assert ws.section_drafts["b"].content == "原始B内容"


def test_apply_section_edit_modes():
    """_apply_section_edit 覆盖三种 mode 与唯一锚点约束。"""
    base = "前段 ANCHOR 后段"

    replaced, ok = WritingAgent._apply_section_edit(
        base, SectionEdit(section_id="s", anchor="ANCHOR", replacement="X", mode="replace")
    )
    assert ok and replaced == "前段 X 后段"

    after, ok = WritingAgent._apply_section_edit(
        base,
        SectionEdit(section_id="s", anchor="ANCHOR", replacement="X", mode="insert_after"),
    )
    assert ok and after == "前段 ANCHORX 后段"

    before, ok = WritingAgent._apply_section_edit(
        base,
        SectionEdit(section_id="s", anchor="ANCHOR", replacement="X", mode="insert_before"),
    )
    assert ok and before == "前段 XANCHOR 后段"

    # 非唯一命中：不应用。
    dup = "词 词"
    unchanged, ok = WritingAgent._apply_section_edit(
        dup, SectionEdit(section_id="s", anchor="词", replacement="X")
    )
    assert not ok and unchanged == dup
