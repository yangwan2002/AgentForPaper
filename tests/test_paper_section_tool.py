"""Round 6 修复回归测试：fetch_paper_section 工具 + ReferenceEntry 段落字段。"""

from __future__ import annotations

from paper_agent.providers.retrieval.openalex import OpenAlexRetrievalProvider
from paper_agent.tools.paper_section_tool import (
    PaperSectionTool,
    extract_section,
    register_paper_section_tool,
)
from paper_agent.tools.registry import ToolRegistry
from paper_agent.workspace.models import (
    InputMode,
    PaperWorkspace,
    ReferenceEntry,
)


# --------------------------------------------------------------------------- #
# ReferenceEntry 新字段 + 序列化
# --------------------------------------------------------------------------- #


def test_reference_entry_new_fields_default():
    ref = ReferenceEntry(
        id="r1", title="T", authors=["A"], year=2020,
        source_id="x", source="arxiv",
    )
    assert ref.pdf_url == ""
    assert ref.abstract_sections == {}


def test_workspace_roundtrip_preserves_pdf_url_and_sections():
    ws = PaperWorkspace(
        workspace_id="w", input_mode=InputMode.GENERATION, topic_background="x"
    )
    ws.verified_references = [
        ReferenceEntry(
            id="r1", title="T", authors=["A"], year=2020,
            source_id="x", source="openalex", verified=True,
            abstract="motivation: foo. method: bar. results: baz.",
            pdf_url="https://example.org/paper.pdf",
            abstract_sections={"method": "bar"},
        )
    ]
    data = ws.to_dict()
    ws2 = PaperWorkspace.from_dict(data)
    ref = ws2.verified_references[0]
    assert ref.pdf_url == "https://example.org/paper.pdf"
    assert ref.abstract_sections == {"method": "bar"}


def test_workspace_from_dict_backward_compat_without_new_fields():
    """旧 JSON 文件无 pdf_url / abstract_sections → 默认空值，不报错。"""
    ws = PaperWorkspace(
        workspace_id="w", input_mode=InputMode.GENERATION, topic_background="x"
    )
    ws.verified_references = [
        ReferenceEntry(
            id="r1", title="T", authors=["A"], year=2020,
            source_id="x", source="arxiv",
        )
    ]
    data = ws.to_dict()
    # 模拟旧版本 JSON：删掉新字段。
    for r in data["verified_references"]:
        r.pop("pdf_url", None)
        r.pop("abstract_sections", None)
    ws2 = PaperWorkspace.from_dict(data)
    assert ws2.verified_references[0].pdf_url == ""
    assert ws2.verified_references[0].abstract_sections == {}


# --------------------------------------------------------------------------- #
# OpenAlex 保留 pdf_url
# --------------------------------------------------------------------------- #


def test_openalex_to_entry_keeps_oa_url():
    work = {
        "id": "https://openalex.org/W42",
        "doi": "https://doi.org/10.1/foo",
        "display_name": "Attention Is All You Need",
        "publication_year": 2017,
        "authorships": [{"author": {"display_name": "Vaswani"}}],
        "open_access": {"oa_url": "https://arxiv.org/pdf/1706.03762.pdf"},
        "primary_location": {"pdf_url": "https://other.example/p.pdf"},
        "abstract_inverted_index": {"hello": [0]},
    }
    ref = OpenAlexRetrievalProvider._to_entry(work)
    # oa_url 优先（更稳定）。
    assert ref.pdf_url == "https://arxiv.org/pdf/1706.03762.pdf"
    assert ref.source == "openalex"


def test_openalex_to_entry_falls_back_to_primary_pdf_url():
    work = {
        "id": "https://openalex.org/W42",
        "doi": "",
        "display_name": "T",
        "publication_year": 2024,
        "open_access": {},
        "primary_location": {"pdf_url": "https://primary.example/p.pdf"},
    }
    ref = OpenAlexRetrievalProvider._to_entry(work)
    assert ref.pdf_url == "https://primary.example/p.pdf"


def test_openalex_to_entry_empty_pdf_url_when_none_available():
    work = {
        "id": "https://openalex.org/W42",
        "doi": "",
        "display_name": "T",
        "publication_year": 2024,
    }
    ref = OpenAlexRetrievalProvider._to_entry(work)
    assert ref.pdf_url == ""


# --------------------------------------------------------------------------- #
# extract_section：结构化命中 + 启发式切片
# --------------------------------------------------------------------------- #


def test_extract_section_structured_hit():
    ref = ReferenceEntry(
        id="r1", title="T", authors=[], year=2020, source_id="x",
        abstract_sections={"method": "we propose Transformer."},
    )
    out = extract_section(ref, "method")
    assert out == "we propose Transformer."


def test_extract_section_case_insensitive_key():
    ref = ReferenceEntry(
        id="r1", title="T", authors=[], year=2020, source_id="x",
        abstract_sections={"Method": "approach details"},
    )
    out = extract_section(ref, "method")
    assert out == "approach details"


def test_extract_section_heuristic_split_method():
    ref = ReferenceEntry(
        id="r1", title="T", authors=[], year=2020, source_id="x",
        abstract=(
            "Background of the field. "
            "Method: we propose a transformer. "
            "Results: we achieve SOTA on multiple benchmarks."
        ),
    )
    out = extract_section(ref, "method")
    assert out is not None
    assert "transformer" in out.lower()
    # 切片应止于 results 关键词之前。
    assert "achieve sota" not in out.lower()


def test_extract_section_returns_none_when_keyword_absent():
    ref = ReferenceEntry(
        id="r1", title="T", authors=[], year=2020, source_id="x",
        abstract="只是一段没有任何段落关键词的散文。",
    )
    assert extract_section(ref, "method") is None


# --------------------------------------------------------------------------- #
# PaperSectionTool：工具调用契约
# --------------------------------------------------------------------------- #


def _ws_with_ref(ref: ReferenceEntry) -> PaperWorkspace:
    ws = PaperWorkspace(
        workspace_id="w", input_mode=InputMode.GENERATION, topic_background="x"
    )
    ws.verified_references = [ref]
    return ws


def test_paper_section_tool_returns_snippet_on_hit():
    ref = ReferenceEntry(
        id="arxiv:1706.03762", title="T", authors=[], year=2017, source_id="1706.03762",
        abstract_sections={"results": "We achieve 28.4 BLEU on WMT'14 EN-DE."},
    )
    tool = PaperSectionTool(_ws_with_ref(ref))
    out = tool.fetch_paper_section("arxiv:1706.03762", "results")
    assert "[arxiv:1706.03762]" in out
    assert "28.4 BLEU" in out


def test_paper_section_tool_unknown_reference_returns_error():
    tool = PaperSectionTool(_ws_with_ref(ReferenceEntry(
        id="r1", title="T", authors=[], year=2020, source_id="x",
    )))
    out = tool.fetch_paper_section("does_not_exist", "method")
    assert "错误" in out
    # 给出可选 id 提示。
    assert "r1" in out


def test_paper_section_tool_unknown_section_returns_available_list():
    ref = ReferenceEntry(
        id="r1", title="T", authors=[], year=2020, source_id="x",
        abstract_sections={"method": "我们提出"},
    )
    tool = PaperSectionTool(_ws_with_ref(ref))
    out = tool.fetch_paper_section("r1", "nonexistent_section")
    assert "错误" in out
    # 应列出可用 section。
    assert "method" in out


def test_paper_section_tool_truncates_to_max_chars():
    long_text = "x" * 5000
    ref = ReferenceEntry(
        id="r1", title="T", authors=[], year=2020, source_id="x",
        abstract_sections={"method": long_text},
    )
    tool = PaperSectionTool(_ws_with_ref(ref), max_chars=200)
    out = tool.fetch_paper_section("r1", "method")
    assert "已截断" in out
    # 输出主体被截断。
    assert len(out) < 5000


def test_paper_section_tool_blank_reference_id_returns_error():
    tool = PaperSectionTool(_ws_with_ref(ReferenceEntry(
        id="r1", title="T", authors=[], year=2020, source_id="x",
    )))
    out = tool.fetch_paper_section("", "method")
    assert "错误" in out


def test_paper_section_tool_blank_section_returns_error():
    tool = PaperSectionTool(_ws_with_ref(ReferenceEntry(
        id="r1", title="T", authors=[], year=2020, source_id="x",
    )))
    out = tool.fetch_paper_section("r1", "")
    assert "错误" in out


# --------------------------------------------------------------------------- #
# register_paper_section_tool 与 ToolRegistry 集成
# --------------------------------------------------------------------------- #


def test_register_paper_section_tool_appears_in_registry():
    ws = _ws_with_ref(ReferenceEntry(
        id="r1", title="T", authors=[], year=2020, source_id="x",
        abstract_sections={"method": "x"},
    ))
    registry = ToolRegistry()
    register_paper_section_tool(registry, ws)
    schemas = registry.to_openai_schemas()
    names = [s["function"]["name"] for s in schemas]
    assert "fetch_paper_section" in names


def test_writing_agent_localized_revision_registers_paper_section_tool():
    """局部修订路径的 registry 应含 fetch_paper_section（Round 6）。"""
    from paper_agent.agents.writing_agent import WritingAgent
    from paper_agent.context.manager import ContextManager
    from paper_agent.providers.llm.mock import MockLLMProvider
    from paper_agent.providers.retrieval.base import RetrievalProvider
    from paper_agent.tools.citation import CitationVerifier

    class _EmptyRetrieval(RetrievalProvider):
        def search(self, query, limit=10):
            return []

        def fetch_metadata(self, identifier):
            return None

    provider = _EmptyRetrieval()
    agent = WritingAgent(
        MockLLMProvider(),
        ContextManager(MockLLMProvider()),
        retrieval=provider,
        verifier=CitationVerifier(provider),
    )
    ws = _ws_with_ref(ReferenceEntry(
        id="r1", title="T", authors=[], year=2020, source_id="x",
        abstract_sections={"method": "x"},
    ))
    registry, _lit, _edit = agent._build_tool_registry(ws)
    names = [s["function"]["name"] for s in registry.to_openai_schemas()]
    assert "fetch_paper_section" in names
