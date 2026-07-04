"""对抗式评审智能体（Round 4：打破自评 reward-hack）。

设计动机：主审 ``ReviewAgent`` 是「主观维度自评打分」，且默认与 writer 共享同一
LLM——这是 reward-hack 的天然温床：模型倾向给「看似合理但平庸」的内容稳定高分，
QualityGate 又只能挡形式硬错误，挡不住学术硬伤（claim 缺证据、novelty 虚假表述、
方法可复现性缺失等）。

本智能体以**默认 reject 立场**评审，要求：

- 必须列出 ≥ ``min_weaknesses`` 条具体、可指认的弱点（不接受空泛措辞）；
- ``decision ∈ {"reject", "borderline", "accept"}``，只有"找不到任何实质性弱点"
  时才能选 ``accept``——单独主审通过不再足以触发达标，必须二者联合（见
  ``Orchestrator._feedback_loop``）。

为最大化对抗效果，装配期通常注入与 writer **不同的** LLM 实例（不同模型/同模型
不同 sampling），见 ``app.build_orchestrator`` 的 ``reviewer_llm_*`` 配置。

与 ``ReviewAgent`` 一样，结构化输出经 ``StructuredParser`` 统一治理，区分
``PARSED / MOCK_FALLBACK / FAILED`` 三态；只有 ``PARSED`` 才参与达标判定。
"""

from __future__ import annotations

from paper_agent.agents.base import Agent, AgentContext, AgentResult
from paper_agent.context.tokenizer import TokenCounter, build_token_counter
from paper_agent.parsing.structured_parser import StructuredParser
from paper_agent.prompts import templates
from paper_agent.providers.llm.base import LLMProvider
from paper_agent.workspace.models import (
    AdversarialReviewRecord,
    ParseStatus,
    PaperWorkspace,
)
from paper_agent.workspace.paper_view import assemble_paper_text

# 评审单次调用的默认 token 预算上限（与 ReviewAgent 一致，复用裁剪逻辑）。
_DEFAULT_REVIEW_TOKEN_BUDGET = 60000

# 合法 decision 取值。
_VALID_DECISIONS = ("reject", "borderline", "accept")

# 合法 severity 取值。
_VALID_SEVERITIES = ("critical", "major", "minor")


class AdversarialReviewAgent(Agent):
    name = "adversarial_review_agent"

    def __init__(
        self,
        llm: LLMProvider,
        *,
        parser: StructuredParser | None = None,
        is_mock: bool = False,
        counter: TokenCounter | None = None,
        review_token_budget: int = _DEFAULT_REVIEW_TOKEN_BUDGET,
        min_weaknesses: int = 3,
    ) -> None:
        self._llm = llm
        self._parser = parser or StructuredParser(llm, is_mock=is_mock)
        self._is_mock = is_mock
        self._counter = counter if counter is not None else build_token_counter()
        self._budget = max(1, review_token_budget)
        self._min_weaknesses = max(1, min_weaknesses)

    def run(self, ctx: AgentContext) -> AgentResult:
        ws = ctx.workspace
        iteration = ws.iteration + 1
        paper_text = self._budgeted_paper_text(ws)

        if not paper_text.strip():
            record = self._failed_record(iteration, reason="空论文，无可评审内容")
            return self._wrap(record, iteration)

        # Round 7：注入 artifact 摘要，让对抗审能识别 fabrication。
        artifact_context = self._artifact_context(ws)

        outcome = self._parser.request_json(
            templates.adversarial_review_paper(
                paper_text=paper_text,
                min_weaknesses=self._min_weaknesses,
                artifact_context=artifact_context,
            ),
            # 仅 decision 必填；weaknesses 在 accept 路径下允许为空列表，由
            # _build_record_from 防御式处理（StructuredParser 把空列表判为空值）。
            required_keys=("decision",),
        )

        if outcome.status is ParseStatus.PARSED:
            record = self._build_record_from(outcome.data or {}, iteration)
            if record is None:
                record = self._failed_record(
                    iteration, reason="对抗式评审输出 decision/weaknesses 字段不可用"
                )
        elif outcome.status is ParseStatus.MOCK_FALLBACK:
            record = self._mock_fallback_record(iteration)
        else:
            record = self._failed_record(
                iteration, reason=outcome.reason or "对抗式评审输出解析失败"
            )

        return self._wrap(record, iteration)

    # --- 结果封装 ---

    def _wrap(
        self, record: AdversarialReviewRecord, iteration: int
    ) -> AgentResult:
        def mutate(w: PaperWorkspace) -> None:
            w.adversarial_records.append(record)

        return AgentResult(
            mutations=[mutate],
            logs=[
                f"第 {iteration} 轮对抗式评审（{record.parse_status.value}）："
                f"decision={record.decision}，weakness={len(record.weaknesses)} 条"
                f"（critical={record.critical_count}）"
            ],
        )

    # --- PARSED 路径 ---

    def _build_record_from(
        self, data: dict, iteration: int
    ) -> AdversarialReviewRecord | None:
        """从 provider 实际返回构建 PARSED 记录。

        弹性容错：
        - decision 不在合法集合 → 视为 "reject"（默认严格）；
        - weaknesses 非列表 → 视为 0 条（→ decision 强制至少 borderline）；
        - severity 非法 → 归入 "minor"，不计入 critical_count。
        """
        decision = str(data.get("decision", "reject")).strip().lower()
        if decision not in _VALID_DECISIONS:
            decision = "reject"

        raw_weaknesses = data.get("weaknesses")
        weaknesses: list[dict] = []
        if isinstance(raw_weaknesses, list):
            for item in raw_weaknesses:
                if not isinstance(item, dict):
                    continue
                issue = str(item.get("issue", "")).strip()
                if not issue:
                    continue  # 空泛/空白条目剔除
                severity = str(item.get("severity", "minor")).strip().lower()
                if severity not in _VALID_SEVERITIES:
                    severity = "minor"
                weaknesses.append(
                    {
                        "section_id": str(item.get("section_id", "")),
                        "category": str(item.get("category", "other")),
                        "severity": severity,
                        "issue": issue,
                        "suggested_fix": str(item.get("suggested_fix", "")),
                    }
                )

        critical_count = sum(
            1 for w in weaknesses if w.get("severity") == "critical"
        )

        # 关键不变式：存在 ≥1 条 weakness 时，decision 至少为 borderline；
        # 仅当 weaknesses 为空时才允许 accept。这是 prompt 的硬约束，但模型未必遵守，
        # 在代码层兜底（打破 reward-hack 的核心防线）。
        if weaknesses and decision == "accept":
            decision = "borderline"

        if not isinstance(data.get("decision"), str) and not weaknesses:
            # decision 与 weaknesses 都不可用 → 视为不可用，让上层走 FAILED。
            return None

        return AdversarialReviewRecord(
            iteration=iteration,
            decision=decision,
            weaknesses=weaknesses,
            critical_count=critical_count,
            parse_status=ParseStatus.PARSED,
        )

    # --- FAILED / MOCK_FALLBACK 路径 ---

    def _failed_record(
        self, iteration: int, reason: str
    ) -> AdversarialReviewRecord:
        """解析失败 → 默认 reject、空 weaknesses，标记 FAILED。

        与 ``ReviewAgent._failed_review`` 同理：失败路径绝不伪造通过决定，
        让反馈循环要么继续迭代、要么以可诊断原因终止。
        """
        safe_reason = (reason or "对抗式评审输出解析失败").strip() or "对抗式评审输出解析失败"
        safe_reason = safe_reason[:500]
        return AdversarialReviewRecord(
            iteration=iteration,
            decision="reject",
            weaknesses=[],
            critical_count=0,
            parse_status=ParseStatus.FAILED,
            unparsed_reason=safe_reason,
        )

    def _mock_fallback_record(self, iteration: int) -> AdversarialReviewRecord:
        """Mock 回退：确定性 reject 决定，使循环可终止但不触发达标。"""
        return AdversarialReviewRecord(
            iteration=iteration,
            decision="reject",
            weaknesses=[
                {
                    "section_id": "",
                    "category": "other",
                    "severity": "minor",
                    "issue": "Mock 评审回退（无实质判断）",
                    "suggested_fix": "",
                }
            ],
            critical_count=0,
            parse_status=ParseStatus.MOCK_FALLBACK,
        )

    # --- 论文文本组装 + 预算裁剪（与 ReviewAgent 行为一致） ---

    @staticmethod
    def _artifact_context(ws: PaperWorkspace) -> str:
        """构造 artifact 摘要（仅当 ws.artifact 存在且非空）。

        与 WritingAgent._artifact_block 类似，但更简洁——对抗审只需知道
        「哪些数字/方法是真实的」，不需要完整实验细节。
        """
        artifact = ws.artifact
        if artifact is None or artifact.is_empty():
            return ""

        sections: list[str] = [
            "【用户提供的真实研究内容（正文必须基于此；数字必须来自下表）】",
            f"研究问题：{artifact.research_question}",
        ]

        if artifact.method.overview:
            sections.append(f"方法概述：\n{artifact.method.overview}")
        if artifact.method.key_components:
            sections.append(
                "关键组件：\n" + "\n".join(f"  - {c}" for c in artifact.method.key_components)
            )

        if artifact.contributions:
            sections.append("贡献：")
            for i, c in enumerate(artifact.contributions, start=1):
                sections.append(f"  {i}. {c.summary}")

        if artifact.experiments:
            sections.append("实验真实数据（正文数字必须能在下表中找到）：")
            for exp in artifact.experiments:
                sections.append(f"  [{exp.experiment_id}] 数据集={exp.dataset}")
                stats = (exp.results_data or {}).get("stats") or {}
                for metric, s in stats.items():
                    if metric in ("n",) or not isinstance(s, dict):
                        continue
                    sections.append(
                        f"    {metric}: mean={s.get('mean')}, "
                        f"std={s.get('std')}, "
                        f"min={s.get('min')}, max={s.get('max')}"
                    )

        if artifact.novelty_claims:
            sections.append(
                "新颖性声明（必须经得起对比验证）：\n"
                + "\n".join(f"  - {c}" for c in artifact.novelty_claims)
            )

        return "\n".join(sections)

    def _budgeted_paper_text(self, ws: PaperWorkspace) -> str:
        full = assemble_paper_text(ws)
        if self._counter.count(full) <= self._budget:
            return full
        nodes = [
            (n, ws.section_drafts.get(n.section_id))
            for n in ws.ordered_sections()
            if ws.section_drafts.get(n.section_id)
        ]
        if not nodes:
            return full
        per = max(1, self._budget // len(nodes))
        note = "\n\n[该章节内容过长已截断，仅评审前半部分]"
        pieces: list[str] = []
        for node, draft in nodes:
            piece = f"## [{node.section_id}] {node.title}\n{draft.content}"
            pieces.append(self._truncate_to_tokens(piece, per, note))
        return "\n\n".join(pieces)

    def _truncate_to_tokens(self, text: str, max_tokens: int, note: str) -> str:
        if max_tokens <= 0:
            return note
        if self._counter.count(text) <= max_tokens:
            return text
        lo, hi, best = 0, len(text), 0
        while lo <= hi:
            mid = (lo + hi) // 2
            if self._counter.count(text[:mid]) <= max_tokens:
                best = mid
                lo = mid + 1
            else:
                hi = mid - 1
        return text[:best] + note
