"""GB/T 7714 文献类型标识（[J]/[C]/[M]...）不被误判为引用编号（trace 暴露的 bug）。"""

from __future__ import annotations

from paper_agent.tools.quality_gate import extract_text_citations


def test_gb7714_markers_not_treated_as_citations():
    text = "王某某. 无人机协同定位[J]. 计算机学报, 2020. 又见会议论文[C]和专著[M]。"
    assert extract_text_citations(text) == []


def test_real_citations_still_extracted():
    text = "如前人工作 [1] 与 [12] 所述，另见 [Smith2020] 和 [arxiv:1706.03762]。"
    assert extract_text_citations(text) == ["1", "12", "Smith2020", "arxiv:1706.03762"]


def test_mixed_markers_and_citations():
    # [J] 是著录标记应剔除；[3] 是真实引用应保留。
    text = "方法参考 [3]，其出处为期刊[J]。"
    assert extract_text_citations(text) == ["3"]


def test_two_letter_doc_markers_excluded():
    text = "电子公告[EB]与数据库[DB]。"
    assert extract_text_citations(text) == []


def test_lowercase_or_mixed_not_excluded():
    # 含小写/数字的 key 不是著录标记，应保留。
    text = "见 [RoMa] 与 [a1]。"
    assert extract_text_citations(text) == ["RoMa", "a1"]
