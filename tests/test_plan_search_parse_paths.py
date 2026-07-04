"""plan / search 解析路径单元测试（Req 3.9，任务 2.8）。

验证 `PlanAgent` 与 `SearchAgent` 统一接入 `StructuredParser` 后，三类来源状态
的行为与改造前的优雅降级语义一致：

1. 成功解析（`PARSED`）：采用 LLM 的结构化结果（大纲 / 检索词 / 相关性过滤）。
2. Mock 回退（`MOCK_FALLBACK`，`is_mock=True` 且输出非 JSON）：回退启发式
   大纲 / 检索词回退主题 / 相关性全保留。
3. 生产失败（`FAILED`，`is_mock=False` 且持续非 JSON、重试至上限）：同样优雅
   降级到启发式大纲 / 检索词回退主题 / 相关性全保留——与改造前一致，不中断。
"""

from __future__ import annotations

import json

from paper_agent.agents.base import AgentContext
from paper_agent.agents.plan_agent import PlanAgent
from paper_agent.agents.search_agent import SearchAgent
from paper_agent.providers.llm.mock import MockLLMProvider
from paper_agent.providers.retrieval.base import RetrievalProvider
from paper_agent.tools.citation import CitationVerifier
from paper_agent.workspace.models import (
    InputMode,
    PaperWorkspace,
    ReferenceEntry,
)


def _run(agent, ws):
    result = agent.run(AgentContext(workspace=ws))
    for m in result.mutations:
        m(ws)
    return result


def _gen_ws(topic="空地slam大视角差图像匹配"):
    return PaperWorkspace(
        workspace_id="w",
        input_mode=InputMode.GENERATION,
        topic_background=topic,
    )


# --------------------------------------------------------------------------- #
# PlanAgent
# --------------------------------------------------------------------------- #


def test_plan_parsed_uses_llm_outline():
    """成功解析（PARSED）：采用 LLM 给出的大纲与检索标记。"""
    scripted = json.dumps(
        {
            "sections": [
                {"section_id": "intro", "title": "引言", "needs_retrieval": False},
                {"section_id": "rw", "title": "相关研究", "needs_retrieval": True},
            ]
        }
    )
    agent = PlanAgent(MockLLMProvider(scripted=[scripted]))
    ws = _gen_ws()
    _run(agent, ws)

    assert [n.title for n in ws.ordered_sections()] == ["引言", "相关研究"]
    rw_task = next(t for t in ws.task_checklist if t.section_ref == "rw")
    assert rw_task.needs_retrieval is True


def test_plan_mock_fallback_uses_heuristic_outline():
    """Mock 回退（is_mock=True 且非 JSON）：回退到启发式默认骨架。"""
    llm = MockLLMProvider(scripted=["这不是合法 JSON 输出"])
    agent = PlanAgent(llm, is_mock=True)
    ws = _gen_ws("主题")
    _run(agent, ws)

    titles = [n.title for n in ws.ordered_sections()]
    assert "引言" in titles and "相关工作" in titles
    # Mock 仅尝试一次即回退，不重试。
    assert len(llm.calls) == 1


def test_plan_production_failure_degrades_to_heuristic_outline():
    """生产失败（is_mock=False 持续非 JSON）：仍优雅降级到启发式骨架。"""
    # 持续非 JSON：重试至上限后 FAILED → 回退启发式骨架。
    llm = MockLLMProvider(scripted=["非 JSON 1", "非 JSON 2"])
    agent = PlanAgent(llm, is_mock=False)
    ws = _gen_ws("主题")
    _run(agent, ws)

    titles = [n.title for n in ws.ordered_sections()]
    assert "引言" in titles and "相关工作" in titles
    # 生产路径会重试：默认 max_parse_retries=1 → 调用 LLM 2 次。
    assert len(llm.calls) == 2


# --------------------------------------------------------------------------- #
# SearchAgent
# --------------------------------------------------------------------------- #


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
        ReferenceEntry(
            id="rel1", title="Air-Ground Cross-View Matching", authors=[],
            year=2023, source_id="d1", source="openalex",
        ),
        ReferenceEntry(
            id="junk1", title="提高初中生英语阅读能力的教学策略", authors=[],
            year=2026, source_id="d2", source="openalex",
        ),
    ]


def test_search_parsed_uses_llm_queries_and_filters():
    """成功解析（PARSED）：用 LLM 英文检索词并据相关性过滤。"""
    provider = _Provider(_entries())
    llm = MockLLMProvider(scripted=[
        json.dumps({"queries": ["air-ground image matching", "UAV UGV SLAM"]}),
        json.dumps({"relevant_ids": ["rel1"]}),
    ])
    agent = SearchAgent(provider, CitationVerifier(provider), llm=llm)
    ws = _gen_ws()
    _run(agent, ws)

    assert "air-ground image matching" in provider.queries
    ids = [r.id for r in ws.verified_references]
    assert ids == ["rel1"]


def test_search_mock_fallback_degrades_queries_and_retains_all():
    """Mock 回退（is_mock=True 且非 JSON）：检索词回退主题、相关性全保留。"""
    provider = _Provider(_entries())
    # 两次解析（检索词 + 相关性）均输出非 JSON。
    llm = MockLLMProvider(scripted=["非 JSON", "非 JSON"])
    agent = SearchAgent(
        provider, CitationVerifier(provider), llm=llm, is_mock=True
    )
    ws = _gen_ws("空地协同")
    _run(agent, ws)

    # 检索词回退为主题原文。
    assert "空地协同" in provider.queries
    # 相关性过滤回退为全保留（两篇均真实）。
    ids = sorted(r.id for r in ws.verified_references)
    assert ids == ["junk1", "rel1"]


def test_search_production_failure_degrades_queries_and_retains_all():
    """生产失败（is_mock=False 持续非 JSON）：检索词回退、相关性全保留，不中断。"""
    provider = _Provider(_entries())
    # 检索词 + 相关性各重试至上限（2 次/路径）均非 JSON → FAILED。
    llm = MockLLMProvider(scripted=["x", "x", "x", "x"])
    agent = SearchAgent(
        provider, CitationVerifier(provider), llm=llm, is_mock=False
    )
    ws = _gen_ws("空地协同")
    _run(agent, ws)

    # 生产失败仍回退主题作为检索词。
    assert "空地协同" in provider.queries
    # 相关性过滤失败 → 全保留，避免误删（与改造前优雅降级一致）。
    ids = sorted(r.id for r in ws.verified_references)
    assert ids == ["junk1", "rel1"]
