"""原创性 / 相似度自检（确定性，无 LLM）。

投稿前的硬风险点之一是正文与已有文献存在大段文本重合（潜在抄袭/自我抄袭）。
本模块提供一个**确定性**的 n-gram 重合度自检：把每个章节正文与工作区中已核验
文献的标题 + 摘要做 n-gram 比对，报告重合比例过高的章节。

设计要点（与既有 quality_gate 同风格）：
- 纯函数、零外部依赖、不调用任何 LLM——只做字符串处理，不 eval/exec。
- 只产出结构化 findings（list[dict]），不改动工作区（供 Orchestrator 记事件/
  并入可投递性判定）。
- 保守：这是**自检提示**而非查重判决——只能检出与「系统检索到的文献」的重合，
  不覆盖全网；因此严重度最高为 medium，用于提醒人工复核，不阻断导出。

重合度定义：section 的 n-gram 集合中，同时出现在任一参考文献文本 n-gram 集合里的
比例（Jaccard 的单边覆盖率 = |S∩R| / |S|）。默认 n=8（词级），阈值 0.15。
"""

from __future__ import annotations

import re

from paper_agent.workspace.models import PaperWorkspace

# 词切分：CJK 按单字、ASCII 按连续字母数字串。既能处理中文也能处理英文正文。
_TOKEN = re.compile(r"[A-Za-z0-9]+|[\u4e00-\u9fff]")

# 参与比对的正文最小 token 数——太短的章节不做重合判定（避免误报）。
_MIN_TOKENS = 40


def tokenize(text: str) -> list[str]:
    """把正文切成小写 token 序列（英文单词小写；中文单字）。"""
    return [m.group(0).lower() for m in _TOKEN.finditer(text or "")]


def ngrams(tokens: list[str], n: int) -> set[tuple[str, ...]]:
    """返回 token 序列的 n-gram 集合（去重）。tokens 短于 n 时返回空集合。"""
    if n <= 0 or len(tokens) < n:
        return set()
    return {tuple(tokens[i : i + n]) for i in range(len(tokens) - n + 1)}


def overlap_ratio(section_text: str, reference_texts: list[str], n: int) -> float:
    """section 的 n-gram 中落在任一参考文献 n-gram 集合内的单边覆盖率。

    返回 [0.0, 1.0]。section n-gram 为空（正文过短）时返回 0.0。
    """
    section_grams = ngrams(tokenize(section_text), n)
    if not section_grams:
        return 0.0
    ref_grams: set[tuple[str, ...]] = set()
    for rt in reference_texts:
        ref_grams |= ngrams(tokenize(rt), n)
    if not ref_grams:
        return 0.0
    hit = len(section_grams & ref_grams)
    return hit / len(section_grams)


def _reference_texts(ws: PaperWorkspace) -> list[str]:
    """收集已核验文献的可比对文本（标题 + 摘要 + 分段摘要）。"""
    texts: list[str] = []
    for r in ws.verified_references:
        if not getattr(r, "verified", False):
            continue
        chunk = " ".join(
            [r.title or "", r.abstract or ""]
            + list((r.abstract_sections or {}).values())
        ).strip()
        if chunk:
            texts.append(chunk)
    return texts


def check_originality(
    ws: PaperWorkspace, *, n: int = 8, threshold: float = 0.15
) -> list[dict]:
    """对每个章节做与已核验文献的 n-gram 重合度自检，返回 findings。

    Args:
        ws: 工作区（读取 section_drafts 与 verified_references）。
        n: n-gram 长度（词级），默认 8。
        threshold: 单边覆盖率阈值，超过即记一条 finding（默认 0.15）。

    Returns:
        list[dict]，每条含 ``type/severity/section_id/overlap/message``。
        无参考文献、正文过短或全部低于阈值时返回空列表。
    """
    reference_texts = _reference_texts(ws)
    if not reference_texts:
        return []

    findings: list[dict] = []
    for node in ws.ordered_sections():
        draft = ws.section_drafts.get(node.section_id)
        if draft is None or not draft.content.strip():
            continue
        tokens = tokenize(draft.content)
        if len(tokens) < _MIN_TOKENS:
            continue
        ratio = overlap_ratio(draft.content, reference_texts, n)
        if ratio >= threshold:
            findings.append(
                {
                    "type": "high_text_overlap",
                    "severity": "medium",
                    "section_id": node.section_id,
                    "overlap": round(ratio, 4),
                    "message": (
                        f"章节《{node.title}》与已检索文献存在较高文本重合"
                        f"（{ratio:.0%} 的 {n}-gram 命中），可能为直接改写，"
                        f"建议人工复核并改写以避免抄袭风险。"
                    ),
                }
            )
    return findings


__all__ = [
    "tokenize",
    "ngrams",
    "overlap_ratio",
    "check_originality",
]
