"""检索地基测试：OpenAlex 摘要重建、arXiv id 解析、元数据核验（不触网）。"""

from __future__ import annotations

from paper_agent.providers.retrieval.api import ArxivRetrievalProvider
from paper_agent.providers.retrieval.base import RetrievalProvider
from paper_agent.providers.retrieval.openalex import (
    OpenAlexRetrievalProvider,
    reconstruct_abstract,
)
from paper_agent.tools.citation import CitationVerifier, title_similarity
from paper_agent.workspace.models import ReferenceEntry


def test_reconstruct_abstract_orders_words():
    inv = {"Attention": [0], "is": [1], "all": [2], "you": [3], "need": [4]}
    assert reconstruct_abstract(inv) == "Attention is all you need"
    assert reconstruct_abstract(None) == ""


def test_openalex_to_entry_parses_fields():
    work = {
        "id": "https://openalex.org/W123",
        "display_name": "A Great Paper",
        "publication_year": 2021,
        "doi": "https://doi.org/10.1/abc",
        "authorships": [{"author": {"display_name": "Jane Doe"}}],
        "abstract_inverted_index": {"Hello": [0], "world": [1]},
    }
    entry = OpenAlexRetrievalProvider._to_entry(work)
    assert entry.title == "A Great Paper"
    assert entry.year == 2021
    assert entry.source_id == "10.1/abc"   # DOI 去前缀
    assert entry.authors == ["Jane Doe"]
    assert entry.abstract == "Hello world"
    assert entry.source == "openalex"


def test_arxiv_id_parsing_preserves_old_category_prefix():
    f = ArxivRetrievalProvider._extract_arxiv_id
    assert f("http://arxiv.org/abs/2509.16909v1") == "2509.16909"
    assert f("http://arxiv.org/abs/1706.03762v5") == "1706.03762"
    # 旧格式必须保留类别前缀（之前的 bug）。
    assert f("http://arxiv.org/abs/physics/0506741") == "physics/0506741"
    assert f("http://arxiv.org/abs/physics/0506741v2") == "physics/0506741"


def test_title_similarity():
    assert title_similarity("Attention Is All You Need", "attention is all you need") > 0.95
    assert title_similarity("Deep SLAM", "Quantum Cooking") < 0.4


class _FakeProvider(RetrievalProvider):
    """按标题返回固定真实记录的假 provider。"""

    def __init__(self, real: ReferenceEntry | None):
        self._real = real

    def search(self, query: str, limit: int = 10):
        return [self._real] if self._real else []

    def fetch_metadata(self, identifier: str):
        return self._real if self._real and identifier == self._real.source_id else None


def test_verify_by_metadata_detects_year_mismatch():
    real = ReferenceEntry(
        id="r", title="Attention Is All You Need", authors=["Vaswani"],
        year=2017, source_id="1706.03762", source="arxiv",
    )
    verifier = CitationVerifier(_FakeProvider(real))
    # 用户初稿里把年份写错成 2020，且没给 source_id。
    entry = ReferenceEntry(
        id="u", title="Attention Is All You Need", authors=["Vaswani"],
        year=2020, source_id="",
    )
    result = verifier.verify_by_metadata(entry)
    assert result.exists is True
    assert result.year_matches is False
    assert "年份" in result.note


def test_verify_by_metadata_flags_nonexistent():
    verifier = CitationVerifier(_FakeProvider(None))
    entry = ReferenceEntry(
        id="u", title="完全不存在的伪造论文标题XYZ", authors=[], year=2099,
        source_id="",
    )
    result = verifier.verify_by_metadata(entry)
    assert result.exists is False
    assert "疑似不存在" in result.note
