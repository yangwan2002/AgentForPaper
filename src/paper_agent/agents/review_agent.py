"""评审智能体（Req 7；升级 Req 1 正确性修复）。

对当前草稿按四个维度（逻辑性/新颖性/论证充分性/语言质量）评分，
给出每个维度的修订建议，以及定位到具体章节的改进点（section_feedback），
并写入评审记录（Req 7.1-7.4）。

结构化输出统一交由 `StructuredParser` 治理（升级 Req 3），据其返回的
`ParseStatus` 区分三条路径（升级 Req 1）：

- ``PARSED``  → `_build_record_from`：分数完全来源于 provider 实际返回。
- ``MOCK_FALLBACK`` → `_mock_fallback_review`：仅测试/Mock provider 的确定性回退，
  产出确定性分数使反馈循环可终止（但 `parse_status != PARSED`，不会触发达标）。
- ``FAILED`` → `_failed_review`：生产环境多次重试后仍解析失败。

**关键修复**：生产环境解析失败时绝不伪造「分数随轮数递增」的达标分数；
失败路径把四个维度分数全部置于严格低于达标阈值的量表下限（0.0），
并以非空 `unparsed_reason` 标识失败类别，迫使再迭代或触达迭代上限。
"""

from __future__ import annotations

from paper_agent.agents.base import Agent, AgentContext, AgentResult
from paper_agent.context.tokenizer import TokenCounter, build_token_counter
from paper_agent.parsing.structured_parser import StructuredParser
from paper_agent.prompts import templates
from paper_agent.providers.llm.base import LLMProvider
from paper_agent.workspace.models import (
    ParseStatus,
    PaperWorkspace,
    ReviewRecord,
    ScoringDimension,
)
from paper_agent.workspace.paper_view import assemble_paper_text

# 评分量表下限：失败/空论文路径将四维度全部置此值，严格低于达标阈值（默认 8.0）。
_SCALE_FLOOR = 0.0

# 评审单次调用的默认 token 预算上限（#8）：长论文不致一次撑爆上下文。
_DEFAULT_REVIEW_TOKEN_BUDGET = 60000


class ReviewAgent(Agent):
    name = "review_agent"

    def __init__(
        self,
        llm: LLMProvider,
        *,
        parser: StructuredParser | None = None,
        is_mock: bool = False,
        base_score: float = 6.0,
        increment: float = 1.0,
        counter: TokenCounter | None = None,
        review_token_budget: int = _DEFAULT_REVIEW_TOKEN_BUDGET,
    ) -> None:
        """构造评审智能体。

        Args:
            llm: 注入的 LLM provider 抽象。
            parser: 结构化解析器；缺省时基于 ``llm`` 构造 ``StructuredParser``。
            is_mock: 是否识别为 Mock/测试 provider（由装配期能力探测决定，
                见 app 装配；此处仅接收参数，默认 ``False``）。
            base_score: Mock 回退路径使用的确定性分数（向后兼容旧构造签名）。
            increment: 向后兼容旧构造签名而保留，已不再用于伪造递增分数。
            counter: token 计量器；缺省构造统一计数器。用于评审文本按预算裁剪。
            review_token_budget: 评审单次调用的 token 预算上限（#8）。超预算时
                按章节均分并截断每节，避免长论文整篇拼接撑爆上下文。
        """
        self._llm = llm
        # is_mock 由解析器实例持有（#12），调用方不再每次 request_json 重复传递。
        self._parser = parser or StructuredParser(llm, is_mock=is_mock)
        self._is_mock = is_mock
        self._base = base_score
        self._increment = increment
        self._counter = counter if counter is not None else build_token_counter()
        self._budget = max(1, review_token_budget)

    def run(self, ctx: AgentContext) -> AgentResult:
        ws = ctx.workspace
        iteration = ws.iteration + 1
        paper_text = self._budgeted_paper_text(ws)

        if not paper_text.strip():
            # 无内容可评：显式非通过，而非伪造分数（Req 1.3）。
            record = self._failed_review(iteration, reason="空论文，无可评审内容")
            return self._wrap(record, iteration)

        outcome = self._parser.request_json(
            templates.review_paper(
                paper_text=paper_text,
                dimensions=[d.value for d in ScoringDimension],
                section_rubrics=self._section_rubrics(ws),
            ),
            required_keys=("scores",),
        )

        if outcome.status is ParseStatus.PARSED:
            record = self._build_record_from(outcome.data or {}, ws, iteration)
            if record is None:
                # 结构在但 scores 不可用（缺失/四维度均非数值）（Req 1.6）。
                record = self._failed_review(
                    iteration, reason="scores 字段缺失或四维度均无法解析为数值"
                )
        elif outcome.status is ParseStatus.MOCK_FALLBACK:
            # 仅测试/骨架：允许确定性回退，使反馈循环可终止（Req 1.5）。
            record = self._mock_fallback_review(iteration)
        else:  # ParseStatus.FAILED —— 生产环境解析失败（Req 1.1）
            record = self._failed_review(
                iteration, reason=outcome.reason or "评审输出解析失败"
            )

        return self._wrap(record, iteration)

    # --- 结果封装 ---

    def _wrap(self, record: ReviewRecord, iteration: int) -> AgentResult:
        def mutate(w: PaperWorkspace) -> None:
            w.review_records.append(record)

        avg = sum(record.scores.values()) / max(1, len(record.scores))
        return AgentResult(
            mutations=[mutate],
            logs=[
                f"第 {iteration} 轮评审（{record.parse_status.value}），"
                f"平均分 {avg:.1f}"
            ],
        )

    # --- PARSED：从 provider 实际返回构建记录 ---

    def _build_record_from(
        self, data: dict, ws: PaperWorkspace, iteration: int
    ) -> ReviewRecord | None:
        """从已解析的 provider 数据构建 PARSED 评审记录。

        当四个维度均无法解析为数值时返回 ``None``，交由上层走失败路径。
        """
        scores = self._parse_scores(data.get("scores"))
        if scores is None:
            return None
        suggestions = self._parse_suggestions(data.get("suggestions"))
        section_feedback = self._parse_section_feedback(
            ws, data.get("section_feedback")
        )
        return ReviewRecord(
            iteration=iteration,
            scores=scores,
            suggestions=suggestions,
            section_feedback=section_feedback,
            parse_status=ParseStatus.PARSED,
        )

    # --- FAILED：生产解析失败 / 空论文 / scores 不可用 ---

    def _failed_review(self, iteration: int, reason: str) -> ReviewRecord:
        """解析失败 → 全维度置于阈值之下的量表下限，标记 FAILED（Req 1.1/1.2/1.3/1.6）。"""
        # unparsed_reason 须为 1-500 字符（Req 1.2）。
        safe_reason = (reason or "评审输出解析失败").strip() or "评审输出解析失败"
        safe_reason = safe_reason[:500]
        return ReviewRecord(
            iteration=iteration,
            scores={dim: _SCALE_FLOOR for dim in ScoringDimension},
            suggestions={
                dim: f"评审输出无法解析（{safe_reason}），本轮不计为达标"
                for dim in ScoringDimension
            },
            parse_status=ParseStatus.FAILED,
            unparsed_reason=safe_reason,
        )

    # --- MOCK_FALLBACK：测试/Mock provider 的确定性回退 ---

    def _mock_fallback_review(self, iteration: int) -> ReviewRecord:
        """Mock/测试 provider 回退：确定性分数，使反馈循环可终止（Req 1.5）。

        分数为确定性常量（不随轮数伪造递增）；因 ``parse_status != PARSED``，
        编排器不会据此判定达标。
        """
        return ReviewRecord(
            iteration=iteration,
            scores={dim: self._base for dim in ScoringDimension},
            suggestions={
                dim: f"提升{dim.value}：补充论据与衔接" for dim in ScoringDimension
            },
            parse_status=ParseStatus.MOCK_FALLBACK,
        )

    # --- 章节体裁评审强制项（Round 8：消费 SectionTypeSpec.review_rubric） ---

    @staticmethod
    def _section_rubrics(ws: PaperWorkspace) -> str:
        """据大纲各章节体裁汇总其 ``review_rubric``，供评审做体裁强制检查。

        去重（同一体裁只列一次）、跳过无 rubric 的体裁；无可列项时返回空串
        （模板据此不注入该块，行为与旧版一致）。
        """
        from paper_agent.prompts.section_types import get_spec, infer_section_type

        seen: set = set()
        lines: list[str] = []
        for node in ws.ordered_sections():
            stype = infer_section_type(node.section_id, node.title)
            if stype in seen:
                continue
            rubric = get_spec(stype).review_rubric
            if rubric:
                seen.add(stype)
                lines.append(f"- 《{node.title}》（{stype.value}）：{rubric}")
        return "\n".join(lines)

    # --- 论文文本组装 ---

    @staticmethod
    def _assemble_paper(ws: PaperWorkspace) -> str:
        # #16：收敛到 workspace.paper_view.assemble_paper_text，统一论文文本口径。
        return assemble_paper_text(ws)

    def _budgeted_paper_text(self, ws: PaperWorkspace) -> str:
        """按 token 预算裁剪论文文本，避免长论文评审单次调用撑爆上下文（#8）。

        未超预算时返回整篇；超预算时按章节均分预算、截断每节前半部分并附备注，
        至少保留每节开头供评审定位。
        """
        full = self._assemble_paper(ws)
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
        """把文本截断到不超过 ``max_tokens`` 个 token，截断时附加 ``note``。"""
        if max_tokens <= 0:
            return note
        if self._counter.count(text) <= max_tokens:
            return text
        # 二分查找 token 数不超过预算的最长字符前缀。
        lo, hi, best = 0, len(text), 0
        while lo <= hi:
            mid = (lo + hi) // 2
            if self._counter.count(text[:mid]) <= max_tokens:
                best = mid
                lo = mid + 1
            else:
                hi = mid - 1
        return text[:best] + note

    # --- 字段解析辅助 ---

    @staticmethod
    def _parse_scores(raw) -> dict[ScoringDimension, float] | None:
        if not isinstance(raw, dict):
            return None
        scores: dict[ScoringDimension, float] = {}
        for dim in ScoringDimension:
            value = raw.get(dim.value)
            if value is None:
                continue
            try:
                scores[dim] = float(value)
            except (TypeError, ValueError):
                continue
        # 至少要解析出一个维度的分数才视为有效。
        return scores or None

    @staticmethod
    def _parse_suggestions(raw) -> dict[ScoringDimension, str]:
        suggestions: dict[ScoringDimension, str] = {}
        if isinstance(raw, dict):
            for dim in ScoringDimension:
                val = raw.get(dim.value)
                if val:
                    suggestions[dim] = str(val)
        return suggestions

    @staticmethod
    def _parse_section_feedback(ws: PaperWorkspace, raw) -> dict[str, str]:
        """把 LLM 返回的章节反馈键归一到真实的 section_id。

        LLM 可能用章节标题或 section_id 作键，这里都映射回 section_id。
        """
        if not isinstance(raw, dict):
            return {}
        title_to_id = {n.title: n.section_id for n in ws.outline}
        valid_ids = {n.section_id for n in ws.outline}
        feedback: dict[str, str] = {}
        for key, val in raw.items():
            if not val:
                continue
            if key in valid_ids:
                feedback[key] = str(val)
            elif key in title_to_id:
                feedback[title_to_id[key]] = str(val)
        return feedback
