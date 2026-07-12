"""引用审计测试：解析、存在性/元数据核验、引用-文献对应。"""

from __future__ import annotations

from paper_agent.agents.base import AgentContext
from paper_agent.agents.citation_audit_agent import CitationAuditAgent
from paper_agent.providers.retrieval.base import RetrievalProvider
from paper_agent.tools.citation import CitationVerifier
from paper_agent.tools.citation_parser import CitationParser
from paper_agent.tools.quality_gate import QualityGate
from paper_agent.workspace.models import (
    InputMode,
    OutlineNode,
    PaperWorkspace,
    ReferenceEntry,
    SectionDraft,
)


def test_parse_in_text_citations():
    parser = CitationParser(llm=None)
    draft = "如文献[1]所述，方法见[2,3]，对比[5-7]。"
    parsed = parser.parse(draft)
    assert parsed.in_text_keys == ["1", "2", "3", "5", "6", "7"]


def test_parse_references_regex_fallback():
    parser = CitationParser(llm=None)  # 无 LLM → 走正则
    draft = (
        "正文引用[1][2]。\n\n"
        "参考文献\n"
        "[1] Vaswani et al. Attention Is All You Need. 2017.\n"
        "[2] Kinney et al. The Semantic Scholar Platform. 2023. 10.1234/abc\n"
    )
    parsed = parser.parse(draft)
    assert len(parsed.references) == 2
    assert parsed.references[0].year == 2017
    assert parsed.references[1].source_id == "10.1234/abc"


class _Provider(RetrievalProvider):
    """返回全部真实记录，由核验器按标题相似度挑选（更接近真实检索行为）。"""

    def __init__(self, real_by_title: dict[str, ReferenceEntry]):
        self._real = real_by_title

    def search(self, query: str, limit: int = 10):
        return list(self._real.values())

    def fetch_metadata(self, identifier: str):
        for e in self._real.values():
            if e.source_id == identifier:
                return e
        return None


def _audit(draft: str, provider: RetrievalProvider) -> PaperWorkspace:
    ws = PaperWorkspace(
        workspace_id="w", input_mode=InputMode.DRAFT_REVISION, original_draft=draft
    )
    # 正则解析的标题含作者/年份噪声，故用较低阈值（生产中 LLM 解析标题更干净）。
    verifier = CitationVerifier(provider, title_threshold=0.6)
    agent = CitationAuditAgent(CitationParser(llm=None), verifier)
    result = agent.run(AgentContext(workspace=ws))
    for m in result.mutations:
        m(ws)
    return ws


def test_audit_flags_nonexistent_and_year_mismatch():
    real = {
        "Attention Is All You Need": ReferenceEntry(
            id="real1", title="Attention Is All You Need", authors=["Vaswani"],
            year=2017, source_id="1706.03762", source="arxiv",
        )
    }
    draft = (
        "见[1]与[2]。\n\n参考文献\n"
        "[1] Vaswani. Attention Is All You Need. 2020.\n"   # 年份写错
        "[2] Nobody. 完全虚构的论文标题不存在. 2099.\n"        # 不存在
    )
    ws = _audit(draft, _Provider(real))
    types = {f["type"] for f in ws.citation_audit}
    assert "metadata" in types     # 年份不符
    assert "existence" in types    # 虚构文献
    # 真实文献已入库供后续引用。
    assert any(r.verified for r in ws.verified_references)
    assert "1" in ws.verified_reference_ids()
    assert "2" not in ws.verified_reference_ids()

    ws.outline = [OutlineNode(section_id="s", title="正文", order=0)]
    ws.draft_sections["s"] = draft
    ws.section_drafts["s"] = SectionDraft(
        section_id="s", title="正文", content=draft
    )
    issues = QualityGate(min_section_chars=1).check(ws).issues
    assert not any(issue["type"] == "text_citation_invalid" for issue in issues)
    assert sum(
        issue["type"] == "source_citation_unverified" for issue in issues
    ) == 1


def test_audit_detects_dangling_citation():
    draft = (
        "正文引用了[1]和[3]。\n\n参考文献\n"
        "[1] Real Paper One. 2020.\n"   # 只有 1 条
    )
    ws = _audit(draft, _Provider({}))
    linkage = [f for f in ws.citation_audit if f["type"] == "linkage"]
    msgs = " ".join(f["message"] for f in linkage)
    assert "悬空引用" in msgs   # 引用了[3]但列表无第3条
