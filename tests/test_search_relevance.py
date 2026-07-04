"""检索智能体：英文检索词生成 + 相关性过滤测试。"""

from __future__ import annotations

import json

from paper_agent.agents.base import AgentContext
from paper_agent.agents.search_agent import SearchAgent
from paper_agent.providers.llm.mock import MockLLMProvider
from paper_agent.providers.retrieval.base import RetrievalProvider
from paper_agent.tools.citation import CitationVerifier
from paper_agent.workspace.models import InputMode, PaperWorkspace, ReferenceEntry


class _Provider(RetrievalProvider):
    def __init__(self, entries):
        self._e = entries
        self._by_id = {e.source_id: e for e in entries}
        self.queries: list[str] = []

    def search(self, query, limit=10):
        self.queries.append(query)
        return list(self._e)[:limit]

    def fetch_metadata(self, identifier):
        return self._by_id.get(identifier)


def _entries():
    return [
        ReferenceEntry(id="rel1", title="Air-Ground Cross-View Matching", authors=[],
                       year=2023, source_id="d1", source="openalex"),
        ReferenceEntry(id="junk1", title="提高初中生英语阅读能力的教学策略", authors=[],
                       year=2026, source_id="d2", source="openalex"),
    ]


def test_search_uses_llm_english_queries():
    provider = _Provider(_entries())
    llm = MockLLMProvider(scripted=[
        json.dumps({"queries": ["air-ground image matching", "UAV UGV SLAM"]}),
        json.dumps({"relevant_ids": ["rel1"]}),
    ])
    agent = SearchAgent(provider, CitationVerifier(provider), llm=llm)
    ws = PaperWorkspace(
        workspace_id="w", input_mode=InputMode.GENERATION,
        topic_background="空地slam大视角差图像匹配",
    )
    result = agent.run(AgentContext(workspace=ws))
    for m in result.mutations:
        m(ws)

    # 用了 LLM 生成的英文检索词，而非中文原题。
    assert "air-ground image matching" in provider.queries
    # 相关性过滤：只保留相关文献，剔除无关的中文教育论文。
    ids = [r.id for r in ws.verified_references]
    assert "rel1" in ids
    assert "junk1" not in ids


def test_search_fallback_without_llm_keeps_all_verified():
    """无 LLM 时：不做相关性过滤，保持向后兼容（全保留已验证）。"""
    provider = _Provider(_entries())
    agent = SearchAgent(provider, CitationVerifier(provider), llm=None)
    ws = PaperWorkspace(
        workspace_id="w", input_mode=InputMode.GENERATION, topic_background="x",
    )
    result = agent.run(AgentContext(workspace=ws))
    for m in result.mutations:
        m(ws)
    assert len(ws.verified_references) == 2  # 两篇都真实 → 都保留


def test_relevance_parse_failure_keeps_all():
    """相关性过滤 LLM 输出不可解析时，全保留以免误删。"""
    provider = _Provider(_entries())
    # 检索词 JSON 正常，但相关性返回非 JSON。
    llm = MockLLMProvider(scripted=[
        json.dumps({"queries": ["q"]}),
        "这不是 JSON",
    ])
    agent = SearchAgent(provider, CitationVerifier(provider), llm=llm)
    ws = PaperWorkspace(
        workspace_id="w", input_mode=InputMode.GENERATION, topic_background="x",
    )
    result = agent.run(AgentContext(workspace=ws))
    for m in result.mutations:
        m(ws)
    assert len(ws.verified_references) == 2
