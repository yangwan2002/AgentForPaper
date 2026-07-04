"""有界 ReAct 工具循环 + 写作期按需检索测试。"""

from __future__ import annotations

from paper_agent.agents.base import AgentContext
from paper_agent.agents.tool_loop import run_tool_loop
from paper_agent.agents.writing_agent import WritingAgent
from paper_agent.context.manager import ContextManager
from paper_agent.providers.llm.base import Message, ToolCall
from paper_agent.providers.llm.mock import MockLLMProvider
from paper_agent.providers.retrieval.base import RetrievalProvider
from paper_agent.tools.citation import CitationVerifier
from paper_agent.tools.literature_tool import build_writing_tools
from paper_agent.tools.registry import ToolRegistry
from paper_agent.workspace.models import (
    InputMode,
    OutlineNode,
    PaperWorkspace,
    ReferenceEntry,
)


def test_tool_loop_executes_tool_then_returns_final():
    """模型先请求工具，工具结果回灌后给出最终正文。"""
    calls = {"n": 0}

    def echo_tool(**kwargs):
        calls["n"] += 1
        return f"工具结果:{kwargs.get('q')}"

    registry = ToolRegistry()
    registry.register(
        "lookup", "查询", echo_tool,
        {"type": "object", "properties": {"q": {"type": "string"}}},
    )
    # 第 1 回合：请求工具；第 2 回合：给最终答案。
    llm = MockLLMProvider(
        scripted=[[ToolCall(id="c1", name="lookup", arguments={"q": "x"})], "最终正文"]
    )
    result = run_tool_loop(llm, [Message("user", "写点东西")], registry, max_iters=3)
    assert result.content == "最终正文"
    assert result.tool_calls_made == 1
    assert calls["n"] == 1


def test_tool_loop_bounded_by_max_iters():
    """模型一直请求工具时，达到上限后强制收尾。"""
    registry = ToolRegistry()
    registry.register("t", "d", lambda **k: "r", {"type": "object", "properties": {}})
    # 始终请求工具（脚本足够多），最后强制无工具调用收尾返回 echo。
    llm = MockLLMProvider(
        scripted=[[ToolCall(id=f"c{i}", name="t", arguments={})] for i in range(10)]
    )
    result = run_tool_loop(llm, [Message("user", "hi")], registry, max_iters=2)
    assert result.tool_calls_made == 2  # 受 max_iters 限制


class _Retrieval(RetrievalProvider):
    def __init__(self, entries):
        self._e = entries
        self._by_id = {e.source_id: e for e in entries}

    def search(self, query, limit=10):
        return list(self._e)[:limit]

    def fetch_metadata(self, identifier):
        return self._by_id.get(identifier)


def test_literature_tool_only_returns_verified():
    real = ReferenceEntry(
        id="arxiv:1706.03762", title="Attention Is All You Need",
        authors=["Vaswani"], year=2017, source_id="1706.03762", source="arxiv",
    )
    provider = _Retrieval([real])
    registry, tool = build_writing_tools(provider, CitationVerifier(provider))
    out = registry.call("search_literature", query="transformer", limit=5)
    assert "Attention Is All You Need" in out
    assert "arxiv:1706.03762" in tool.found
    assert tool.found["arxiv:1706.03762"].verified is True


def test_writing_agent_tool_mode_searches_and_stores_refs():
    """写作 agent 在工具模式下：模型调检索 → 文献入库 → 正文生成。"""
    real = ReferenceEntry(
        id="arxiv:1706.03762", title="Attention Is All You Need",
        authors=["Vaswani"], year=2017, source_id="1706.03762", source="arxiv",
    )
    provider = _Retrieval([real])
    verifier = CitationVerifier(provider)

    # 单章节；脚本：写该章时先检索，再产出正文。
    llm = MockLLMProvider(
        scripted=[
            [ToolCall(id="c1", name="search_literature",
                      arguments={"query": "transformer attention"})],
            "这是引言正文，引用了 [arxiv:1706.03762]。",
            "引言摘要",  # summarize_section 调用
        ]
    )
    agent = WritingAgent(
        llm, ContextManager(MockLLMProvider()),
        retrieval=provider, verifier=verifier,
    )
    ws = PaperWorkspace(
        workspace_id="w", input_mode=InputMode.GENERATION, topic_background="x"
    )
    ws.outline = [OutlineNode(section_id="intro", title="引言", order=0)]

    result = agent.run(AgentContext(workspace=ws))
    for m in result.mutations:
        m(ws)

    # 写作期检索到的真实文献已入库。
    assert any(r.id == "arxiv:1706.03762" and r.verified for r in ws.verified_references)
    # 该章节引用了它。
    assert "arxiv:1706.03762" in ws.section_drafts["intro"].cited_reference_ids
