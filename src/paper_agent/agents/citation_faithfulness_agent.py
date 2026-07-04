"""引用忠实性审计智能体（citation-faithfulness-audit）。

本模块承载声明级 grounded 引用校验的判定层。本次仅实现：

- ``severity_for``：裁决 → 严重度的全函数映射（对 ``FaithfulnessVerdict`` 四值全覆盖）。
- ``FaithfulnessJudge``：注入式 LLM-as-judge，经 ``StructuredParser.request_json``
  产出结构化裁决，并对一切非 ``PARSED`` 路径安全降级到 ``cannot_verify``。

后续任务（7.1）将在本文件继续实现 ``CitationFaithfulnessAgent``，编排抽取 /
grounding 组装 / 判定 / 报告，收敛为单条 mutation 写入工作区。

设计不变量（核心安全属性）：
- **grounding-only**：判定输入仅由 ``claim`` + ``grounding`` + ``reference_meta``
  构成，不含其它章节正文或模型记忆提示（Req 3.1）。
- **绝不假 supported**：唯有 ``ParseStatus.PARSED`` 且 verdict 属于枚举时才可能
  返回 ``supported`` / ``weak_support``；``MOCK_FALLBACK`` / ``FAILED`` / 异常
  一律落 ``cannot_verify``（Req 3.4/3.5/3.6）。
- **依赖倒置**：``StructuredParser`` 经构造注入，不在内部实例化（Req 9.2）。
"""

from __future__ import annotations

from paper_agent.agents.base import Agent, AgentContext, AgentResult
from paper_agent.observability.events import Event, EventKind, EventSink
from paper_agent.parsing.structured_parser import StructuredParser
from paper_agent.prompts import templates
from paper_agent.tools.faithfulness_extract import extract_pairs
from paper_agent.tools.faithfulness_grounding import assemble_grounding
from paper_agent.workspace.faithfulness import (
    CitationFaithfulnessFinding,
    ClaimCitationPair,
    FaithfulnessVerdict,
)
from paper_agent.workspace.models import ParseStatus, PaperWorkspace, ReferenceEntry

# 观测/摘要文本片段长度上限（脱敏：日志与 claim_excerpt 均施加上限，Req 5.1/7.5）。
_CLAIM_EXCERPT_MAX = 200
_OBS_SNIPPET_MAX = 160
# reference_meta 中纳入的作者数量上限（仅取 title/year/authors，绝不含其它正文）。
_META_AUTHOR_LIMIT = 3

# 裁决 → 严重度的全函数映射（Req 4.2/4.3/4.4/4.5）。
_SEVERITY_BY_VERDICT: dict[FaithfulnessVerdict, str] = {
    FaithfulnessVerdict.UNSUPPORTED: "high",
    FaithfulnessVerdict.WEAK_SUPPORT: "medium",
    FaithfulnessVerdict.CANNOT_VERIFY: "low",
    FaithfulnessVerdict.SUPPORTED: "none",  # 不计为需修订的问题（Req 4.5）
}


def severity_for(verdict: FaithfulnessVerdict) -> str:
    """将忠实性裁决映射为严重度（全函数，覆盖枚举四值）。

    - ``unsupported`` → ``"high"``
    - ``weak_support`` → ``"medium"``
    - ``cannot_verify`` → ``"low"``
    - ``supported`` → ``"none"``（不计为需修订问题）
    """
    return _SEVERITY_BY_VERDICT[verdict]


class FaithfulnessJudge:
    """LLM-as-judge 忠实性判定器（注入 ``StructuredParser``）。

    仅接收「声明句 + grounding 文本 + 被引文献元信息」，经 ``StructuredParser``
    产出结构化裁决；对一切非 ``PARSED`` 路径安全降级到 ``cannot_verify``，
    永不在非 ``PARSED`` 时返回 ``supported`` / ``weak_support``。
    """

    def __init__(self, parser: StructuredParser) -> None:
        # 依赖注入：不在内部实例化具体解析器（Req 9.2）。
        self._parser = parser

    def judge(
        self, *, claim: str, grounding: str, reference_meta: str
    ) -> tuple[FaithfulnessVerdict, str, str, ParseStatus]:
        """判定某声明句是否被其被引文献支撑。

        返回 ``(verdict, rationale, supporting_snippet, parse_status)``。

        Postconditions:
            - ``status == PARSED`` ⟹ verdict 取自 ``data['verdict']``，非法枚举值
              经 ``FaithfulnessVerdict(...)`` 的 ``_missing_`` 回落 ``cannot_verify``；
            - ``status in {MOCK_FALLBACK, FAILED}`` ⟹ ``cannot_verify``，rationale
              记录降级原因（Req 3.4/3.5）；
            - ``request_json`` 抛异常 ⟹ 视为 ``FAILED`` → ``cannot_verify``，不向上
              传播（Req 7.6 的单对异常隔离由调用方进一步兜底）。
            - 绝不在非 ``PARSED`` 时返回 ``supported`` / ``weak_support``（Req 3.6）。
        """
        messages = templates.judge_citation_faithfulness(
            claim=claim, grounding=grounding, reference_meta=reference_meta
        )
        try:
            outcome = self._parser.request_json(
                messages, required_keys=("verdict",)
            )
        except Exception as exc:  # noqa: BLE001 - 判定失败绝不冒泡中止管线
            # request_json 抛异常：等同解析失败，安全降级（Req 7.6）。
            return (
                FaithfulnessVerdict.CANNOT_VERIFY,
                f"judge_error: {type(exc).__name__}",
                "",
                ParseStatus.FAILED,
            )

        if outcome.status == ParseStatus.PARSED:
            data = outcome.data or {}
            # 非法/未知 verdict 经枚举 _missing_ 回落 cannot_verify（Req 3.3）。
            verdict = FaithfulnessVerdict(data.get("verdict"))
            rationale = data.get("rationale", "")
            supporting_snippet = data.get("supporting_snippet", "")
            return (verdict, rationale, supporting_snippet, ParseStatus.PARSED)

        # 非 PARSED（MOCK_FALLBACK / FAILED）：绝不 supported/weak_support（Req 3.4/3.5/3.6）。
        reason = outcome.reason or outcome.status.value
        return (
            FaithfulnessVerdict.CANNOT_VERIFY,
            f"parse_{outcome.status.value}: {reason}",
            "",
            outcome.status,
        )


def _truncate(text: str, limit: int) -> str:
    """防御式截断纯字符串（治理未知/超长文本，绝不 eval/exec，Req 7.3）。"""
    if not text:
        return ""
    return text if len(text) <= limit else text[:limit]


class CitationFaithfulnessAgent(Agent):
    """引用忠实性审计智能体：抽取 → grounding → 判定 → 报告（Req 7.1）。

    编排三个纯逻辑子步骤 + 一个注入的判定器，全部产出收敛为**单条** mutation
    写入 ``ws.citation_faithfulness``（单一写入路径，Req 5.2 / 9.1；替换而非累加，
    Req 5.5）。``run`` 本身不改动传入工作区，绝不向上抛异常（Req 7.6）。

    关键不变量：
    - 未验证 id 直接成 ``cannot_verify`` finding（``unverified_reference=True``），
      不调判定器（Req 1.5）。
    - grounding 去空白后为空或 ``< min_grounding_chars`` → ``cannot_verify``，
      不调判定器（Req 2.5）。
    - 逐对 ``try/except`` 隔离：单对异常记 ``cannot_verify`` 并 ``continue``（Req 7.6）。
    - 严重度经全函数 ``severity_for`` 映射（Req 4）。
    """

    name = "citation_faithfulness_agent"

    def __init__(
        self,
        judge: FaithfulnessJudge,
        *,
        min_grounding_chars: int,
        token_budget: int,
        is_mock: bool = False,
        sink: EventSink | None = None,
    ) -> None:
        # 依赖倒置：判定器经构造注入，不在内部实例化（Req 9.2）。
        self._judge = judge
        self._min_grounding_chars = min_grounding_chars
        self._token_budget = token_budget
        self._is_mock = is_mock
        # sink 为 None 时静默跳过观测（无可观测开销，保持向后兼容）。
        self._sink = sink

    def run(self, ctx: AgentContext) -> AgentResult:
        ws = ctx.workspace
        logs: list[str] = []
        findings: list[CitationFaithfulnessFinding] = []

        verified_ids = ws.verified_reference_ids()
        # id -> ReferenceEntry 索引，供 grounding 组装快速查表。
        ref_by_id = {r.id: r for r in ws.verified_references}

        section_count = len(ws.section_drafts)
        self._emit(
            f"忠实性审计开始：章节 {section_count} 个，已验证文献 {len(verified_ids)} 条"
        )

        for section_id, draft in ws.section_drafts.items():
            content = getattr(draft, "content", "") or ""
            verified_pairs, unverified_pairs = extract_pairs(
                section_id, content, verified_ids
            )

            # 未验证引用：直接成 cannot_verify，不调判定器（Req 1.5）。
            for pair in unverified_pairs:
                findings.append(
                    self._finding(
                        pair,
                        verdict=FaithfulnessVerdict.CANNOT_VERIFY,
                        rationale="cited_reference_id 不在已验证文献库",
                        supporting_snippet="",
                        parse_status="n/a",
                        unverified_reference=True,
                    )
                )

            # 已验证引用：组装 grounding → 充足性短路 → 判定。逐对异常隔离（Req 7.6）。
            for pair in verified_pairs:
                findings.append(self._judge_pair(pair, ref_by_id))

        report = [f.to_dict() for f in findings]

        def mutate(w: PaperWorkspace) -> None:
            # 单一写入路径：替换而非累加（Req 5.2 / 5.5 / 9.1）。
            w.citation_faithfulness = report

        unsupported = sum(1 for f in findings if f.verdict is FaithfulnessVerdict.UNSUPPORTED)
        cannot = sum(1 for f in findings if f.verdict is FaithfulnessVerdict.CANNOT_VERIFY)
        summary = (
            f"忠实性审计完成：发现 {len(findings)} 条"
            f"（unsupported={unsupported}, cannot_verify={cannot}）"
        )
        logs.append(summary)
        self._emit(summary)
        return AgentResult(mutations=[mutate], logs=logs)

    def _judge_pair(
        self,
        pair: ClaimCitationPair,
        ref_by_id: dict[str, ReferenceEntry],
    ) -> CitationFaithfulnessFinding:
        """组装 grounding 并判定单个已验证对；全程异常隔离（Req 7.6）。"""
        try:
            ref = ref_by_id.get(pair.cited_reference_id)
            if ref is None:
                # 防御：extract_pairs 认定已验证但索引缺失，安全落 cannot_verify。
                return self._finding(
                    pair,
                    verdict=FaithfulnessVerdict.CANNOT_VERIFY,
                    rationale="未在已验证文献库中找到对应文献记录",
                    supporting_snippet="",
                    parse_status="n/a",
                )

            grounding = assemble_grounding(ref, token_budget=self._token_budget)
            # grounding 不足前置短路：不调判定器（Req 2.5）。
            if not grounding.strip() or len(grounding) < self._min_grounding_chars:
                return self._finding(
                    pair,
                    verdict=FaithfulnessVerdict.CANNOT_VERIFY,
                    rationale="grounding 文本不足（为空或低于最小字符阈值）",
                    supporting_snippet="",
                    parse_status="n/a",
                )

            # 防御式截断：喂入判定器的 claim 亦受 token_budget 上限（Req 2.6 / 7.4）。
            claim = _truncate(pair.claim_sentence, self._token_budget)
            reference_meta = self._reference_meta(ref)
            verdict, rationale, supporting_snippet, parse_status = self._judge.judge(
                claim=claim, grounding=grounding, reference_meta=reference_meta
            )
            return self._finding(
                pair,
                verdict=verdict,
                rationale=rationale,
                supporting_snippet=supporting_snippet,
                parse_status=parse_status.value,
            )
        except Exception as exc:  # noqa: BLE001 - 单对异常绝不冒泡中止整次审计
            reason = f"pair_error: {type(exc).__name__}: {exc}"
            self._emit(f"单对判定异常，降级 cannot_verify：{_truncate(reason, _OBS_SNIPPET_MAX)}")
            return self._finding(
                pair,
                verdict=FaithfulnessVerdict.CANNOT_VERIFY,
                rationale=_truncate(reason, _CLAIM_EXCERPT_MAX),
                supporting_snippet="",
                parse_status="n/a",
            )

    def _finding(
        self,
        pair: ClaimCitationPair,
        *,
        verdict: FaithfulnessVerdict,
        rationale: str,
        supporting_snippet: str,
        parse_status: str,
        unverified_reference: bool = False,
    ) -> CitationFaithfulnessFinding:
        """按裁决构造发现；severity 经全函数 ``severity_for`` 映射（Req 4）。"""
        return CitationFaithfulnessFinding(
            section_id=pair.section_id,
            cited_reference_id=pair.cited_reference_id,
            claim_excerpt=_truncate(pair.claim_sentence, _CLAIM_EXCERPT_MAX),
            verdict=verdict,
            severity=severity_for(verdict),
            rationale=rationale or "",
            supporting_snippet=supporting_snippet or "",
            parse_status=parse_status,
            unverified_reference=unverified_reference,
        )

    @staticmethod
    def _reference_meta(ref: ReferenceEntry) -> str:
        """从文献自身构造简短元信息串（仅 title/year/authors，绝不含其它正文）。"""
        authors = ", ".join((ref.authors or [])[:_META_AUTHOR_LIMIT])
        year = "" if ref.year is None else str(ref.year)
        meta = f"标题: {ref.title or ''}; 年份: {year}; 作者: {authors}"
        return _truncate(meta, _CLAIM_EXCERPT_MAX)

    def _emit(self, message: str) -> None:
        """经既有 EventSink 发结构化观测日志；文本片段施加长度上限（脱敏，Req 7.5）。"""
        if self._sink is None:
            return
        self._sink.emit(
            Event(
                kind=EventKind.AGENT_LOG,
                message=_truncate(message, _OBS_SNIPPET_MAX),
                data={"agent": self.name, "is_mock": self._is_mock},
            )
        )


__all__ = ["severity_for", "FaithfulnessJudge", "CitationFaithfulnessAgent"]
