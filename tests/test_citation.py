"""引用真实性硬约束测试（Req 4 / Property 1, 2）。"""

from __future__ import annotations

from paper_agent.agents.base import AgentContext
from paper_agent.agents.search_agent import SearchAgent
from paper_agent.providers.retrieval.mock import MockRetrievalProvider
from paper_agent.tools.citation import CitationVerifier
from paper_agent.workspace.models import (
    InputMode,
    PaperWorkspace,
    ReferenceEntry,
)


def test_verifier_rejects_unknown_and_empty_source_id():
    provider = MockRetrievalProvider()
    verifier = CitationVerifier(provider)

    real = ReferenceEntry(
        id="x", title="Attention Is All You Need", authors=["Vaswani"],
        year=2017, source_id="1706.03762", source="arxiv",
    )
    fake = ReferenceEntry(
        id="y", title="伪造文献", authors=[], year=2099, source_id="0000.00000",
    )
    empty = ReferenceEntry(
        id="z", title="无标识符", authors=[], year=None, source_id="",
    )

    assert verifier.verify(real) is True
    assert verifier.verify(fake) is False     # 检索源查无此文 → 拒绝
    assert verifier.verify(empty) is False     # 无 source_id → 拒绝


def test_search_agent_only_admits_verified_references():
    """Property 2：仅核验通过的文献进入已验证文献库。"""
    # provider 中只有合法样例；注入一条伪造文献作为候选不会命中 fetch_metadata。
    provider = MockRetrievalProvider()
    verifier = CitationVerifier(provider)
    agent = SearchAgent(provider, verifier)

    ws = PaperWorkspace(
        workspace_id="w", input_mode=InputMode.GENERATION,
        topic_background="transformer",
    )
    result = agent.run(AgentContext(workspace=ws))
    for mutation in result.mutations:
        mutation(ws)

    assert len(ws.verified_references) > 0
    assert all(r.verified for r in ws.verified_references)
    assert all(r.source_id for r in ws.verified_references)
