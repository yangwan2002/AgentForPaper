"""Property-based tests for citation-faithfulness-audit · PairExtractor 抽取阶段。

- Property 1: 引用扫描规则复用一致性（Req 1.1 / 9.3）——``extract_pairs`` 抽取到的
  引用 id 集合与 ``quality_gate.extract_text_citations`` 逐一一致（复用同一条正则）。
- Property 2: 声明句包含其标注且为完整句子（Req 1.2 / 1.3）——每个 ``ClaimCitationPair``
  的 ``claim_sentence`` 含其 ``[id]`` 标注，且恰为 ``split_sentences`` 中覆盖该标注
  字符位置的确定性句子。
- Property 3: 同句多引用逐一成对且去重（Req 1.4）——单个句子内每个**不同**
  ``cited_reference_id`` 恰产一对；重复 ``(claim_sentence, cited_reference_id)`` 去重。

生成器约束（对齐既有 props 测试）：任意 ``st.text`` 一律排除 unicode 代理区 "Cs"
与控制字符 "Cc"；自由填充文本还排除方括号，避免污染引用集合比较；引用 id 取自
``quality_gate._TEXT_CITATION`` 实际认可的字符类（``[A-Za-z0-9_.:\\-]``）。
"""

from __future__ import annotations

from hypothesis import assume, given, settings
from hypothesis import strategies as st

from paper_agent.tools.faithfulness_extract import extract_pairs, split_sentences
from paper_agent.tools.quality_gate import (
    _TEXT_CITATION,
    _is_doc_type_marker,
    extract_text_citations,
)

# --------------------------------------------------------------------------- #
# 生成器
# --------------------------------------------------------------------------- #

# 句子边界字符（与 faithfulness_extract._SENTENCE_BOUNDARIES 一致）。
_BOUNDARY_CHARS = "。！？.!?\n\r"

# 引用 id：_TEXT_CITATION 认可的字符类 [A-Za-z0-9_.:\-]。
# _SIMPLE_ID 不含点（'.' 是句子边界），供 Property 3 保持"单句"不变。
_SIMPLE_ID = st.text(
    alphabet="abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_:-",
    min_size=1,
    max_size=8,
)
# arXiv 风格含点 id，如 "2301.12345"（点会被句子切分器当作边界——覆盖已知边缘用例）。
_DOTTED_ID = st.builds(
    lambda a, b: f"{a}.{b}",
    st.integers(min_value=1000, max_value=9999),
    st.integers(min_value=10000, max_value=99999),
)
_ANY_ID = st.one_of(_SIMPLE_ID, _DOTTED_ID)

# 自由填充文本：可含句子边界（构造多句正文），但排除方括号避免误造引用标注。
_FILLER = st.text(
    alphabet=st.characters(
        blacklist_categories=("Cs", "Cc"), blacklist_characters="[]"
    ),
    max_size=20,
)
# 单句填充：额外排除所有句子边界字符，保证不引入新句子。
_FILLER_NO_BOUNDARY = st.text(
    alphabet=st.characters(
        blacklist_categories=("Cs", "Cc"),
        blacklist_characters="[]" + _BOUNDARY_CHARS,
    ),
    max_size=15,
)


def _marker(id_strat):
    """将 id 策略包装为 ``[id]`` 标注文本。"""
    return id_strat.map(lambda i: f"[{i}]")


@st.composite
def _non_citation_bracket(draw):
    """生成**不应**被 _TEXT_CITATION 匹配的方括号（含空格/CJK）。

    紧跟 ``[`` 的字符是空格，空格不在 id 字符类中，因此整段不构成引用标注。
    用于验证 Property 1 的"扫描规则一致"在非引用括号面前依然成立。
    """
    inner = draw(
        st.text(
            alphabet=st.characters(
                blacklist_categories=("Cs", "Cc"), blacklist_characters="[]"
            ),
            max_size=10,
        )
    )
    return f"[ {inner}]"


@st.composite
def _content(draw, id_strat=_ANY_ID):
    """交错拼接自由文本、真引用标注与非引用括号，构造真实感正文。

    覆盖：无标注、单标注、同句多标注、重复 id、非引用括号等情形。
    """
    segments = draw(
        st.lists(
            st.one_of(_FILLER, _marker(id_strat), _non_citation_bracket()),
            max_size=8,
        )
    )
    return "".join(segments)


@st.composite
def _single_sentence(draw):
    """构造保证为**单个句子**的正文：无任何边界字符、id 不含点。

    从一个小 id 池采样以刻意制造重复 id（触发去重路径）。
    """
    pool = draw(st.lists(_SIMPLE_ID, min_size=1, max_size=4, unique=True))
    n = draw(st.integers(min_value=0, max_value=8))
    parts: list[str] = []
    for _ in range(n):
        if pool and draw(st.booleans()):
            parts.append(f"[{draw(st.sampled_from(pool))}]")
        else:
            parts.append(draw(_FILLER_NO_BOUNDARY))
    return "".join(parts)


def _enclosing_sentence(sentences, pos):
    """复刻 faithfulness_extract._find_enclosing_sentence，用于独立验证确定性。"""
    for start, end, text in sentences:
        if start <= pos < end:
            return text
    return sentences[-1][2] if sentences else ""


# --------------------------------------------------------------------------- #
# Property 1: 引用扫描规则复用一致性
# --------------------------------------------------------------------------- #

# Feature: citation-faithfulness-audit, Property 1: 引用扫描规则复用一致性
@given(content=_content(), verified=st.sets(_ANY_ID, max_size=4))
@settings(max_examples=100)
def test_prop1_scan_rule_reuse_consistency(content, verified):
    """Validates: Requirements 1.1, 9.3"""
    verified_pairs, unverified_pairs = extract_pairs("sec", content, verified)

    ids_from_pairs = {p.cited_reference_id for p in verified_pairs} | {
        p.cited_reference_id for p in unverified_pairs
    }
    ids_from_gate = set(extract_text_citations(content))

    # 抽取对（两列表合并）的 id 集合恰等于 quality_gate 的扫描结果集合。
    assert ids_from_pairs == ids_from_gate


# --------------------------------------------------------------------------- #
# Property 2: 声明句包含其标注且为完整句子
# --------------------------------------------------------------------------- #

# Feature: citation-faithfulness-audit, Property 2: 声明句包含其标注且为完整句子
@given(content=_content(), verified=st.sets(_ANY_ID, max_size=4))
@settings(max_examples=100)
def test_prop2_claim_sentence_contains_marker_and_is_full_sentence(content, verified):
    """Validates: Requirements 1.2, 1.3"""
    verified_pairs, unverified_pairs = extract_pairs("sec", content, verified)
    all_pairs = verified_pairs + unverified_pairs

    # 用与被测代码相同的受保护区间（[id] 标注跨度）切分，确保标注不被跨句切开。
    protected = [(m.start(), m.end()) for m in _TEXT_CITATION.finditer(content)]
    sentences = split_sentences(content, protected)
    produced = {(p.claim_sentence, p.cited_reference_id) for p in all_pairs}

    # 确定性：每个 [id] 标注的 claim_sentence 恰为覆盖其字符位置的 split_sentences 句子。
    # GB/T 7714 文献类型标识（[J]/[C]/[A] 等）按契约不算引用、不产 pair，与 extract_pairs
    # 一致地跳过（扫描规则复用一致性）。
    for match in _TEXT_CITATION.finditer(content):
        ref_id = match.group(1)
        if _is_doc_type_marker(ref_id):
            continue
        expected_sentence = _enclosing_sentence(sentences, match.start())
        assert (expected_sentence, ref_id) in produced

    # 声明句必须完整包含其 [id] 标注。
    for pair in all_pairs:
        assert f"[{pair.cited_reference_id}]" in pair.claim_sentence


# --------------------------------------------------------------------------- #
# Property 3: 同句多引用逐一成对且去重
# --------------------------------------------------------------------------- #

# Feature: citation-faithfulness-audit, Property 3: 同句多引用逐一成对且去重
@given(content=_single_sentence())
@settings(max_examples=100)
def test_prop3_multi_citation_one_pair_per_distinct_id(content):
    """Validates: Requirements 1.4"""
    # 构造保证 ≤ 1 个句子（无边界字符）。
    assume(len(split_sentences(content)) <= 1)

    verified_pairs, unverified_pairs = extract_pairs("sec", content, set())
    all_pairs = verified_pairs + unverified_pairs

    distinct_ids = set(extract_text_citations(content))

    # 每个不同 id 恰产一对：对数 == 不同 id 数。
    assert len(all_pairs) == len(distinct_ids)

    # 覆盖到全部不同 id（逐一成对）。
    ids_in_pairs = [p.cited_reference_id for p in all_pairs]
    assert sorted(ids_in_pairs) == sorted(distinct_ids)

    # (claim_sentence, cited_reference_id) 去重：无重复键。
    keys = [(p.claim_sentence, p.cited_reference_id) for p in all_pairs]
    assert len(keys) == len(set(keys))
