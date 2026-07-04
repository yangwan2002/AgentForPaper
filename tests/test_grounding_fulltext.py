"""Round 9：正文级 grounding + 被引文献正文富化。"""

from __future__ import annotations

from paper_agent.tools.faithfulness_grounding import assemble_grounding
from paper_agent.tools.reference_enrichment import collect_full_texts
from paper_agent.workspace.models import ReferenceEntry


def _ref(**kw) -> ReferenceEntry:
    base = dict(
        id="r1", title="Cross-view matching", authors=["A"], year=2020,
        source_id="x", source="arxiv", verified=True,
        abstract="We study aerial-ground matching.",
    )
    base.update(kw)
    return ReferenceEntry(**base)


# --- 正文级 grounding ---


def test_grounding_without_fulltext_unchanged():
    ref = _ref()
    g = assemble_grounding(ref, token_budget=4000)
    # 无 full_text → 仍只含 title + abstract。
    assert "Cross-view matching" in g
    assert "aerial-ground matching" in g
    assert "hierarchical alignment" not in g


def test_grounding_uses_fulltext_body():
    body = (
        "Introduction: background on matching. "
        "Method: we propose a hierarchical alignment network with learned descriptors. "
        "Results: strong performance is observed on the benchmark dataset."
    )
    ref = _ref(full_text=body)
    g = assemble_grounding(ref, token_budget=8000)
    # 正文里的方法/结果细节现在进入 grounding（abstract 没有这些）。
    assert "hierarchical alignment network" in g
    assert "strong performance" in g


def test_grounding_backward_compatible_bytes():
    """full_text 为空时，与不含该字段的旧行为逐字节一致。"""
    ref_no = _ref()
    ref_empty = _ref(full_text="")
    assert assemble_grounding(ref_no, token_budget=4000) == assemble_grounding(
        ref_empty, token_budget=4000
    )


# --- 富化收集（注入 stub fetcher）---


class _StubFetcher:
    def __init__(self, mapping):
        self._mapping = mapping
        self.calls = []

    def fetch(self, url):
        self.calls.append(url)
        return self._mapping.get(url)


def test_collect_full_texts_fills_from_pdf_url():
    refs = [
        _ref(id="a", pdf_url="http://x/a.pdf"),
        _ref(id="b", pdf_url="http://x/b.pdf"),
        _ref(id="c", pdf_url=""),  # 无 url → 跳过
    ]
    fetcher = _StubFetcher({"http://x/a.pdf": "full body A", "http://x/b.pdf": "full body B"})
    out = collect_full_texts(refs, fetcher, max_refs=20)
    assert out == {"a": "full body A", "b": "full body B"}
    assert "http://x/a.pdf" in fetcher.calls


def test_collect_skips_already_enriched_and_unverified():
    refs = [
        _ref(id="a", pdf_url="http://x/a.pdf", full_text="already"),  # 已有正文 → 跳过
        _ref(id="b", pdf_url="http://x/b.pdf", verified=False),        # 未验证 → 跳过
    ]
    fetcher = _StubFetcher({"http://x/a.pdf": "new", "http://x/b.pdf": "new"})
    out = collect_full_texts(refs, fetcher, max_refs=20)
    assert out == {}


def test_collect_respects_max_refs_and_fetch_failure():
    refs = [_ref(id=f"r{i}", pdf_url=f"http://x/{i}.pdf") for i in range(5)]
    # 只有前两个能取到；max_refs=2 限制调用。
    fetcher = _StubFetcher({"http://x/0.pdf": "b0", "http://x/1.pdf": "b1",
                            "http://x/2.pdf": "b2"})
    out = collect_full_texts(refs, fetcher, max_refs=2)
    assert len(out) <= 2


def test_reference_full_text_serialization_roundtrip():
    from paper_agent.workspace.models import (
        InputMode,
        PaperWorkspace,
    )

    ws = PaperWorkspace(workspace_id="w", input_mode=InputMode.GENERATION)
    ws.verified_references.append(_ref(full_text="body text here"))
    restored = PaperWorkspace.from_dict(ws.to_dict())
    assert restored.verified_references[0].full_text == "body text here"


def test_old_json_without_full_text_key_defaults_empty():
    from paper_agent.workspace.models import InputMode, PaperWorkspace

    ws = PaperWorkspace(workspace_id="w", input_mode=InputMode.GENERATION)
    ws.verified_references.append(_ref())
    d = ws.to_dict()
    # 模拟旧 JSON：删掉 full_text 键。
    for r in d["verified_references"]:
        r.pop("full_text", None)
    restored = PaperWorkspace.from_dict(d)
    assert restored.verified_references[0].full_text == ""
