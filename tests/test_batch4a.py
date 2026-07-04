"""批次 4A 回归测试：citation 缓存/重试、glossary 排序、docx ins/del + 表格段报告。"""

from __future__ import annotations

import pytest

from paper_agent.providers.retrieval.base import RetrievalError
from paper_agent.tools.citation import CitationVerifier
from paper_agent.workspace.models import ReferenceEntry


def _entry(**kw):
    base = dict(id="r1", title="Attention Is All You Need", authors=["V"],
                year=2017, source_id="arxiv:1706.03762")
    base.update(kw)
    return ReferenceEntry(**base)


class _CountingProvider:
    """记录调用次数、可编程为「前 k 次抛错」的检索 provider 桩。"""

    def __init__(self, *, fail_times=0, found=True):
        self.meta_calls = 0
        self.search_calls = 0
        self._fail_times = fail_times
        self._found = found

    def fetch_metadata(self, source_id):
        self.meta_calls += 1
        if self.meta_calls <= self._fail_times:
            raise RetrievalError("transient")
        return _entry() if self._found else None

    def search(self, title, limit=5):
        self.search_calls += 1
        if self.search_calls <= self._fail_times:
            raise RetrievalError("transient")
        return [_entry()]


# --- citation 缓存 ---


def test_verify_caches_metadata():
    prov = _CountingProvider()
    v = CitationVerifier(prov)
    assert v.verify(_entry()) is True
    assert v.verify(_entry()) is True  # 第二次应命中缓存
    assert prov.meta_calls == 1


# --- citation 有界重试 ---


def test_verify_retries_transient_then_succeeds():
    prov = _CountingProvider(fail_times=2)  # 前 2 次抖动，第 3 次成功
    v = CitationVerifier(prov, max_retries=3, retry_backoff=0.0)
    assert v.verify(_entry()) is True
    assert prov.meta_calls == 3  # 2 失败 + 1 成功


def test_verify_gives_up_after_retries():
    prov = _CountingProvider(fail_times=99)
    v = CitationVerifier(prov, max_retries=2, retry_backoff=0.0)
    # 耗尽重试仍失败 → verify 捕获 RetrievalError 返回 False。
    assert v.verify(_entry()) is False
    assert prov.meta_calls == 3  # max_retries+1


# --- glossary 排序稳定前缀 ---


def test_stable_block_glossary_sorted():
    from paper_agent.context.manager import ContextManager
    from paper_agent.providers.llm.mock import MockLLMProvider
    from paper_agent.workspace.models import InputMode, PaperWorkspace

    ws = PaperWorkspace(workspace_id="w", input_mode=InputMode.GENERATION)
    # 乱序插入，渲染应按 key 排序。
    ws.glossary["zeta"] = "z"
    ws.glossary["alpha"] = "a"
    ws.glossary["mu"] = "m"
    cm = ContextManager(MockLLMProvider())
    block = cm.stable_block(ws)
    assert "alpha=a；mu=m；zeta=z" in block


# --- docx ins/del 与结构 part 签名 ---


def test_ins_del_in_special_names():
    from paper_agent.docx_inplace import _SPECIAL_LOCAL_NAMES

    assert "ins" in _SPECIAL_LOCAL_NAMES
    assert "del" in _SPECIAL_LOCAL_NAMES


def test_docx_reports_skipped_table_prose(tmp_path):
    docx = pytest.importorskip("docx")
    from paper_agent.docx_inplace import InplaceDocxPolisher
    from paper_agent.providers.llm.base import LLMResponse

    src = str(tmp_path / "in.docx")
    out = str(tmp_path / "out.docx")
    d = docx.Document()
    d.add_paragraph(
        "A normal body prose paragraph long enough to be considered for polishing here."
    )
    t = d.add_table(rows=1, cols=1)
    t.cell(0, 0).text = (
        "This is a fairly long prose paragraph living inside a table cell in the doc."
    )
    d.save(src)

    class _NoopLLM:
        def complete(self, messages, **opts):
            return LLMResponse(content="")  # 空 → 守卫拒绝，不改文本

    result = InplaceDocxPolisher(_NoopLLM(), is_mock=False).polish(src, out)
    assert any("表格内段落" in n for n in result.notes)
    assert not result.rolled_back


def test_structural_part_shas_reads_parts(tmp_path):
    docx = pytest.importorskip("docx")
    from paper_agent.docx_inplace import _structural_part_shas

    src = str(tmp_path / "in.docx")
    d = docx.Document()
    d.add_paragraph("hello world")
    d.save(src)
    shas = _structural_part_shas(src)
    # 至少应含 styles.xml（python-docx 默认模板带样式）。
    assert any("styles.xml" in k for k in shas)
