"""增量写路径的忠实性筛查适配器。

把既有的声明级引用忠实性审计（``extract_pairs`` + ``assemble_grounding`` +
``FaithfulnessJudge``）适配为 ``GuardrailGate`` 需要的 ``FaithfulnessScreener``：
给定（dry-run 的）工作区与目标章节，返回该章节里「引用造假」的原因列表。

严格的放行原则（对齐与用户的约定，避免误杀）：
- **只检查已带引用 `[id]` 且引用已核验的句子**——不带引用的句子完全不看，绝不
  要求任何句子添加引用；未核验引用由质量闸另行处理（此处不重复拦）。
- **只有裁决为 UNSUPPORTED（引用与声明明确不符）才作为阻断原因**。
- **拿不到文献支撑材料（grounding 不足）或裁决为 CANNOT_VERIFY / SUPPORTED /
  WEAK_SUPPORT 一律放行**——查不了就不拦，只拦真造假。
- 任一句判定异常都吞掉并放行（防御式，绝不因内部错误误杀）。
"""

from __future__ import annotations

import time

from paper_agent.agents.citation_faithfulness_agent import FaithfulnessJudge
from paper_agent.tools.faithfulness_extract import extract_pairs
from paper_agent.tools.faithfulness_grounding import assemble_grounding
from paper_agent.workspace.faithfulness import FaithfulnessVerdict
from paper_agent.workspace.models import PaperWorkspace, ReferenceEntry

_CLAIM_EXCERPT = 60
_META_AUTHOR_LIMIT = 3

# 单次 screen 的默认核验预算——每句都串行调 LLM judge，大章节（如整章 add_section）
# 会累积到几十次调用、上百秒，造成"卡住"体验。用条数 + 墙钟双预算封顶：超出即停止
# 核验剩余句并放行（符合既定"查不了就放行、只拦真造假"原则；主防线另有质量闸兜底）。
_DEFAULT_MAX_CLAIMS = 12
_DEFAULT_SCREEN_DEADLINE_S = 30.0


class GuardrailFaithfulnessScreener:
    """供 GuardrailGate 使用的忠实性筛查器（只拦 UNSUPPORTED，查不了放行）。"""

    def __init__(
        self,
        judge: FaithfulnessJudge,
        *,
        min_grounding_chars: int,
        token_budget: int,
        max_claims: int = _DEFAULT_MAX_CLAIMS,
        screen_deadline_s: float = _DEFAULT_SCREEN_DEADLINE_S,
    ) -> None:
        self._judge = judge
        self._min_grounding_chars = min_grounding_chars
        self._token_budget = token_budget
        # 单次 screen 的核验预算：最多核验 max_claims 句、总耗时不超过
        # screen_deadline_s 秒；超出即停止核验剩余句并放行（避免大章节落盘卡住）。
        # <=0 表示不限（保留旧行为，供严格模式）。
        self._max_claims = max_claims
        self._screen_deadline_s = screen_deadline_s

    def unsupported_reasons(
        self, ws: PaperWorkspace, section_id: str
    ) -> list[str]:
        """返回目标章节里「引用明确不支撑声明」的原因；查不了/无引用则返回空。

        受单次核验预算约束（条数 + 墙钟）：预算用尽即停止核验剩余句并放行——保证
        落盘（尤其整章 add_section）快速返回，不因逐句串行 LLM 核验卡住。
        """
        targets = self._target_sections(ws, section_id)
        if not targets:
            return []

        verified_ids = ws.verified_reference_ids()
        ref_by_id = {r.id: r for r in ws.verified_references}
        reasons: list[str] = []

        start = time.monotonic()
        checked = 0
        for sid, draft in targets:
            content = getattr(draft, "content", "") or ""
            verified_pairs, _unverified = extract_pairs(sid, content, verified_ids)
            for pair in verified_pairs:
                if self._budget_exhausted(checked, start):
                    return reasons  # 预算用尽 → 放行剩余（宁可漏检不卡住）
                checked += 1
                reason = self._judge_pair(pair, ref_by_id)
                if reason:
                    reasons.append(reason)
        return reasons

    def _budget_exhausted(self, checked: int, start: float) -> bool:
        """是否已达单次 screen 的条数或墙钟预算（<=0 表示该维度不限）。"""
        if self._max_claims > 0 and checked >= self._max_claims:
            return True
        if self._screen_deadline_s > 0 and (
            time.monotonic() - start
        ) > self._screen_deadline_s:
            return True
        return False

    def _target_sections(self, ws: PaperWorkspace, section_id: str):
        """section_id 命中则只查该章节；为空则查全部章节（整篇改动场景）。"""
        if section_id:
            draft = ws.section_drafts.get(section_id)
            return [(section_id, draft)] if draft is not None else []
        return list(ws.section_drafts.items())

    def _judge_pair(self, pair, ref_by_id: dict[str, ReferenceEntry]) -> str | None:
        """判定单个「已核验引用」的声明句；仅 UNSUPPORTED 返回原因，否则 None。"""
        try:
            ref = ref_by_id.get(pair.cited_reference_id)
            if ref is None:
                return None  # 查不到文献记录 → 放行（不臆断）

            grounding = assemble_grounding(ref, token_budget=self._token_budget)
            if not grounding.strip() or len(grounding) < self._min_grounding_chars:
                return None  # 无足够支撑材料 → 放行（cannot_verify，不误杀）

            verdict, rationale, _snippet, _status = self._judge.judge(
                claim=pair.claim_sentence[: self._token_budget],
                grounding=grounding,
                reference_meta=self._reference_meta(ref),
            )
            if verdict is FaithfulnessVerdict.UNSUPPORTED:
                claim = pair.claim_sentence.strip()[:_CLAIM_EXCERPT]
                return (
                    f"声明「{claim}…」所引文献[{pair.cited_reference_id}]并不支撑该说法"
                    f"（{rationale or '判定为不支撑'}）——请改为有据的表述或换用支撑该说法的文献。"
                )
            return None  # SUPPORTED / WEAK_SUPPORT / CANNOT_VERIFY → 放行
        except Exception:  # noqa: BLE001 - 判定异常绝不误杀，一律放行
            return None

    @staticmethod
    def _reference_meta(ref: ReferenceEntry) -> str:
        authors = ", ".join((ref.authors or [])[:_META_AUTHOR_LIMIT])
        year = "" if ref.year is None else str(ref.year)
        return f"标题: {ref.title or ''}; 年份: {year}; 作者: {authors}"[:_CLAIM_EXCERPT * 4]


__all__ = ["GuardrailFaithfulnessScreener"]
