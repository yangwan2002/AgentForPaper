"""共享的润色保真守卫（供语言润色 / LaTeX 原地润色复用，避免重复实现）。

润色只应改语言，不得动事实、数据、引用与结构。本模块把「润色前后必须保持一致」
的确定性判据集中于此：

- ``content_preserved``：引用标注 ``[id]`` 集合 + 数字多重集合完全一致
  （散文层守卫——语言润色、LaTeX 散文片段共用）。
- ``latex_structure_preserved``：在此之上追加 LaTeX 结构不变——反斜杠命令多重
  集合 + ``{}[]$`` 计数完全一致（仅 LaTeX 原地润色需要）。
- ``length_ratio_ok``：润色后长度浮动在允许区间内（防止整段重写/大幅删改）。

任一守卫不通过即应「丢弃润色、保留原文」。纯字符串处理，不调用 LLM、不 eval/exec。
"""

from __future__ import annotations

import re

from paper_agent.tools.quality_gate import extract_text_citations

# 数字：可带负号的整数或小数（不含千分位）。用于「润色不得增删/改动任何数字」判定。
# 带负号 → 捕获 ``-3.2`` 为独立 token，模型把 ``-3.2`` 翻转成 ``3.2`` 会破坏多重集合
# → 守卫拦截（保守方向：宁可拒绝也不放过数值改动）。
_NUMBER = re.compile(r"-?\d+(?:\.\d+)?")
# 反斜杠命令 token：\word（含可选 *）或 \<单个非字母>（如 \% \& \\）。
_BACKSLASH_TOKEN = re.compile(r"\\[A-Za-z]+\*?|\\[^A-Za-z]")

DEFAULT_MIN_LEN_RATIO = 0.5
DEFAULT_MAX_LEN_RATIO = 2.0


def numeric_multiset(text: str) -> tuple[str, ...]:
    """正文中所有数字（整数/小数）的排序多重集合。"""
    return tuple(sorted(_NUMBER.findall(text or "")))


def citation_set(text: str) -> frozenset[str]:
    """正文中所有 ``[id]`` 引用标注的集合（复用质量闸口径）。"""
    return frozenset(extract_text_citations(text or ""))


def backslash_multiset(text: str) -> tuple[str, ...]:
    """LaTeX 反斜杠命令 token 的排序多重集合（无序）。"""
    return tuple(sorted(_BACKSLASH_TOKEN.findall(text or "")))


def backslash_sequence(text: str) -> tuple[str, ...]:
    """LaTeX 反斜杠命令 token 的**有序**序列。

    比多重集合更严：可捕获 ``\\emph{A}...\\textbf{B}`` 被换成
    ``\\textbf{A}...\\emph{B}`` 这类「命令顺序改变、语义改变」的破坏。
    """
    return tuple(_BACKSLASH_TOKEN.findall(text or ""))


def bracket_dollar_counts(text: str) -> tuple[int, ...]:
    """``{`` ``}`` ``[`` ``]`` ``$`` 各自的出现次数（LaTeX 结构指纹的一部分）。"""
    text = text or ""
    return tuple(text.count(ch) for ch in "{}[]$")


def length_ratio_ok(
    original: str,
    candidate: str,
    *,
    lo: float = DEFAULT_MIN_LEN_RATIO,
    hi: float = DEFAULT_MAX_LEN_RATIO,
) -> bool:
    """润色后长度相对原文的比值是否落在 ``[lo, hi]``。原文去空白后为空 → False。"""
    orig_len = len((original or "").strip())
    if orig_len == 0:
        return False
    return lo <= len(candidate or "") / orig_len <= hi


def content_preserved(original: str, candidate: str) -> bool:
    """散文层守卫：引用集合与数字多重集合完全一致（不丢、不增、不改）。"""
    return (
        citation_set(original) == citation_set(candidate)
        and numeric_multiset(original) == numeric_multiset(candidate)
    )


def latex_structure_preserved(original: str, candidate: str) -> bool:
    """LaTeX 结构守卫：反斜杠命令**有序序列**与 ``{}[]$`` 计数完全一致。

    用有序序列（而非多重集合）以额外拦截命令重排导致的语义改变。
    """
    return (
        backslash_sequence(original) == backslash_sequence(candidate)
        and bracket_dollar_counts(original) == bracket_dollar_counts(candidate)
    )


__all__ = [
    "numeric_multiset",
    "citation_set",
    "backslash_multiset",
    "backslash_sequence",
    "bracket_dollar_counts",
    "length_ratio_ok",
    "content_preserved",
    "latex_structure_preserved",
    "DEFAULT_MIN_LEN_RATIO",
    "DEFAULT_MAX_LEN_RATIO",
]
