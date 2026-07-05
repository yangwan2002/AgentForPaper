"""声明-引用对抽取（citation-faithfulness-audit · PairExtractor）。

纯函数、确定性、零 I/O、零 LLM。职责：

- ``split_sentences``：确定性句子切分，供定位每个 ``[id]`` 标注所在的完整声明句。
- ``extract_pairs``：**复用** ``quality_gate._TEXT_CITATION`` 这一**同一条**已编译正则
  （不新增第二套引用扫描实现，Req 1.1 / 9.3）定位每个标注的字符位置，再用
  ``split_sentences`` 找到包含该位置的完整句子作为 ``claim_sentence``，产出
  ``ClaimCitationPair``；按 ``ref_id`` 是否属于 ``verified_ids`` 分区为
  (verified_pairs, unverified_pairs)。

设计契约（对齐 design.md 的 PairExtractor 小节）：
- 引用扫描规则与 ``quality_gate.extract_text_citations`` 逐字一致——直接复用同一
  ``re.Pattern`` 对象做 ``finditer``（Req 1.1）。
- 句子切分为确定性纯函数：边界 = CJK ``。！？`` + ASCII ``.!?`` + 换行；连续边界
  折叠；句子含其尾随边界标点（Req 1.3）。
- 同句多个**不同** id 各产一对；``(claim_sentence, ref_id)`` 去重（Req 1.2 / 1.4）。
- ``ref_id ∉ verified_ids`` 进入 ``unverified_pairs``（Req 1.5）。
- 空正文 / 无 ``[id]`` → 两个空列表（Req 1.6）。
"""

from __future__ import annotations

from collections.abc import Iterable

from paper_agent.tools.quality_gate import _TEXT_CITATION, _is_non_citation_marker
from paper_agent.workspace.faithfulness import ClaimCitationPair

# 句子边界字符：CJK 句末标点 + ASCII 句末标点 + 换行/回车（Req 1.3）。
_SENTENCE_BOUNDARIES = frozenset("。！？.!?\n\r")


def split_sentences(
    text: str,
    protected_spans: Iterable[tuple[int, int]] | None = None,
) -> list[tuple[int, int, str]]:
    """确定性句子切分。

    返回每个句子的 ``(start, end, sentence_text)``，其中 ``sentence_text`` 恰为
    ``text[start:end]``（原文切片，保留原始偏移）。规则：

    - 边界字符 = ``。！？`` + ``.!?`` + 换行/回车。
    - 连续边界折叠：多个相邻边界并入**同一**句子的尾随标点段，不产生空句。
    - 每个句子**包含**其尾随边界标点。
    - 结尾若无终止边界，则最后一段也作为一个句子返回。
    - 空字符串 → 空列表。

    切分结果**连续无缝**覆盖 ``[0, len(text))``（相邻句 ``end`` 即下一句 ``start``），
    因此任一字符位置恰落入唯一一个句子。

    ``protected_spans`` 为可选的 ``(start, end)`` 半开区间序列（例如引用标注
    ``[id]`` 的字符跨度）。落入任一受保护区间内的边界字符**不**被视为句子边界，
    从而保证一个 ``[id]`` 标注绝不会被切分到两个句子中（Req 1.2 / 1.3）。默认
    ``None`` 时行为与旧版逐字一致（向后兼容），受保护集合为空时结果 byte-for-byte
    相同。
    """
    if not text:
        return []

    n = len(text)

    # 将受保护区间展开为受保护字符位置集合；这些位置上的边界字符被忽略。
    protected: set[int] = set()
    if protected_spans is not None:
        for span_start, span_end in protected_spans:
            lo = max(0, span_start)
            hi = min(n, span_end)
            protected.update(range(lo, hi))

    def _is_boundary(idx: int) -> bool:
        return text[idx] in _SENTENCE_BOUNDARIES and idx not in protected

    sentences: list[tuple[int, int, str]] = []
    start = 0
    i = 0
    while i < n:
        if _is_boundary(i):
            # 折叠连续边界，全部并入当前句子的尾随段。
            j = i + 1
            while j < n and _is_boundary(j):
                j += 1
            sentences.append((start, j, text[start:j]))
            start = j
            i = j
        else:
            i += 1

    # 结尾无终止边界的残余片段。
    if start < n:
        sentences.append((start, n, text[start:n]))

    return sentences


def _find_enclosing_sentence(
    sentences: list[tuple[int, int, str]], pos: int
) -> str:
    """返回 ``[start, end)`` 包含字符位置 ``pos`` 的句子文本。

    由于 ``split_sentences`` 的输出连续覆盖整段文本，正常情况下 ``pos`` 必落入
    某一句子。作为防御，若因边界情况未命中则回退到最后一个句子。
    """
    for start, end, sentence in sentences:
        if start <= pos < end:
            return sentence
    return sentences[-1][2] if sentences else ""


def extract_pairs(
    section_id: str,
    content: str,
    verified_ids: set[str],
) -> tuple[list[ClaimCitationPair], list[ClaimCitationPair]]:
    """抽取声明-引用对，按引用是否已验证分区。

    - 复用 ``quality_gate._TEXT_CITATION``（同一条已编译正则）对 ``content`` 做
      ``finditer``，取每个 ``[id]`` 标注的字符位置与 id。
    - 每个标注经 ``_find_enclosing_sentence`` 定位其所在完整句子作 ``claim_sentence``。
    - 去重：一对 ``(claim_sentence, cited_reference_id)`` 只产一个 ``ClaimCitationPair``。
    - ``cited_reference_id`` 属于 ``verified_ids`` → ``verified_pairs``；否则
      → ``unverified_pairs``。
    - ``content`` 为空或无 ``[id]`` → ``([], [])``。

    抽取到的 id 集合（两个列表合并）逐一等于
    ``quality_gate.extract_text_citations(content)`` 返回的 id 集合。
    """
    verified_pairs: list[ClaimCitationPair] = []
    unverified_pairs: list[ClaimCitationPair] = []

    if not content:
        return verified_pairs, unverified_pairs

    # 从**同一条**引用扫描正则计算受保护区间：任何落入 [id] 标注跨度内的边界字符
    # 都不得作为句子边界，确保每个标注完整落入唯一一个句子（Req 1.2 / 1.3）。
    protected = [(m.start(), m.end()) for m in _TEXT_CITATION.finditer(content)]
    sentences = split_sentences(content, protected)
    seen: set[tuple[str, str]] = set()

    for match in _TEXT_CITATION.finditer(content):
        ref_id = match.group(1)
        # 与 extract_text_citations 复用同一排除规则：GB/T 7714 著录类型标识（[J]/[C]/[M]
        # 等）与 LaTeX 交叉引用标签（[eq:..]/[tab:..] 等）都不是引用编号，三处必须一致。
        if _is_non_citation_marker(ref_id):
            continue
        sentence = _find_enclosing_sentence(sentences, match.start())

        key = (sentence, ref_id)
        if key in seen:
            continue
        seen.add(key)

        pair = ClaimCitationPair(
            section_id=section_id,
            claim_sentence=sentence,
            cited_reference_id=ref_id,
        )
        if ref_id in verified_ids:
            verified_pairs.append(pair)
        else:
            unverified_pairs.append(pair)

    return verified_pairs, unverified_pairs
