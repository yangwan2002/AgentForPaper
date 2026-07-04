"""Unit tests for `faithfulness_extract.extract_pairs` (citation-faithfulness-audit).

Task 3.5 / Req 1.6：抽取阶段对以下输入不产任何对（或仅对真正匹配引用正则的标注产对）：

- 空正文 → `([], [])`。
- 无 `[id]` 标注的正文 → `([], [])`。
- 含非引用方括号（如 `[表格 第1页]` / `[see appendix]` / `[图 2]`）——这些含空格/CJK
  的方括号不被 `quality_gate._TEXT_CITATION` 视作引用 id，故不产对。
- 正向对照：真实 `[id]` 标注，id 属于 verified_ids → verified_pairs；否则 →
  unverified_pairs。

所有断言与 `quality_gate.extract_text_citations` 的引用识别规则对齐，确保「非引用方括号」
样例确实被同一条正则拒绝（Req 1.1 / 9.3 复用一致性）。
"""

from __future__ import annotations

from paper_agent.tools.faithfulness_extract import extract_pairs
from paper_agent.tools.quality_gate import extract_text_citations
from paper_agent.workspace.faithfulness import ClaimCitationPair


def test_empty_content_returns_two_empty_lists():
    """Req 1.6：空正文 → `([], [])`。"""
    verified, unverified = extract_pairs("sec", "", {"r1", "r2"})

    assert verified == []
    assert unverified == []


def test_content_without_any_markers_returns_two_empty_lists():
    """Req 1.6：无任何 `[id]` 标注的正文 → `([], [])`。"""
    content = "这是一段没有任何引用标注的正文。It contains no bracketed citations at all."
    # 前置校验：确认这段正文对同一条引用正则确实零命中。
    assert extract_text_citations(content) == []

    verified, unverified = extract_pairs("sec", content, {"r1"})

    assert verified == []
    assert unverified == []


def test_non_citation_brackets_produce_no_pairs():
    """含空格/CJK 的方括号不是引用 id，应不产对。

    `[表格 第1页]` / `[see appendix]` / `[图 2]` 均含空格且/或 CJK 字符，不满足
    `_TEXT_CITATION`（限 ASCII 标识符字符），故 `extract_text_citations` 零命中，
    `extract_pairs` 也不产任何对。
    """
    content = (
        "如下所示 [表格 第1页] 给出结果。"
        "For details [see appendix]。"
        "参见 [图 2] 的示意。"
    )
    # 引用正则对这些非引用方括号确实零命中——样例是有效的负例。
    assert extract_text_citations(content) == []

    verified, unverified = extract_pairs("sec", content, {"r1", "r2"})

    assert verified == []
    assert unverified == []


def test_non_citation_brackets_mixed_with_real_citation_only_yields_real():
    """混入非引用方括号与一个真实 `[id]`：只对真实标注产对。

    验证正则确实只挑出真正匹配的 id，而非因非引用方括号而误产对。
    """
    content = "见 [表格 第1页] 与 [图 2]；结论依据 [smith2020] 的工作。"
    # 同一条引用正则只识别出真实 id。
    assert extract_text_citations(content) == ["smith2020"]

    verified, unverified = extract_pairs("sec", content, {"smith2020"})

    assert len(verified) == 1
    assert unverified == []
    assert verified[0].cited_reference_id == "smith2020"


def test_verified_id_goes_to_verified_pairs():
    """正向对照：id ∈ verified_ids → verified_pairs。"""
    content = "我们的方法优于基线 [smith2020]。"

    verified, unverified = extract_pairs("sec-1", content, {"smith2020"})

    assert unverified == []
    assert len(verified) == 1
    pair = verified[0]
    assert isinstance(pair, ClaimCitationPair)
    assert pair.section_id == "sec-1"
    assert pair.cited_reference_id == "smith2020"
    assert "smith2020" in pair.claim_sentence


def test_unverified_id_goes_to_unverified_pairs():
    """正向对照：id ∉ verified_ids → unverified_pairs。"""
    content = "我们的方法优于基线 [ghost99]。"

    verified, unverified = extract_pairs("sec-1", content, {"smith2020"})

    assert verified == []
    assert len(unverified) == 1
    pair = unverified[0]
    assert pair.section_id == "sec-1"
    assert pair.cited_reference_id == "ghost99"
    assert "ghost99" in pair.claim_sentence


def test_mixed_verified_and_unverified_partition():
    """混合：已验证与未验证 id 分别进入对应分区，且合并的 id 集合与正则识别一致。"""
    content = "对比结果 [smith2020] 与 [ghost99] 表明差异显著。"
    verified_ids = {"smith2020"}

    verified, unverified = extract_pairs("sec-2", content, verified_ids)

    assert {p.cited_reference_id for p in verified} == {"smith2020"}
    assert {p.cited_reference_id for p in unverified} == {"ghost99"}

    # 合并两分区的 id 集合应等于同一条引用正则识别出的 id 集合（复用一致性）。
    combined = {p.cited_reference_id for p in verified + unverified}
    assert combined == set(extract_text_citations(content))
