"""检索智能体（Req 3 / 4）。

流程：主题 → 生成英文检索词 → 检索候选 → 真实性核验 → 相关性过滤 → 入库。

两道闸门缺一不可：
- 真实性（Req 4）：必须经 CitationVerifier 核验存在。
- 相关性：必须与研究主题相关（用 LLM 判断），剔除"真实但无关"的文献——
  这是之前参考文献"牛头不对马嘴"的根因修复。

LLM 不可用时优雅降级：检索词回退为主题原文，相关性过滤回退为全保留。
LLM 可用时，JSON 解析统一经 `StructuredParser` 治理（Req 3.9）：仅当解析状态为
`PARSED` 时采用其结果，`MOCK_FALLBACK` / `FAILED` 一律按同一回退语义降级
（检索词回退原文、相关性过滤全保留），不再散落静默 `extract_json` 回退。
"""

from __future__ import annotations

from paper_agent.agents.base import Agent, AgentContext, AgentResult
from paper_agent.parsing import StructuredParser
from paper_agent.prompts import templates
from paper_agent.providers.llm.base import LLMProvider
from paper_agent.providers.retrieval.base import RetrievalError, RetrievalProvider
from paper_agent.tools.citation import CitationVerifier
from paper_agent.workspace.models import ParseStatus, PaperWorkspace, ReferenceEntry


class SearchAgent(Agent):
    name = "search_agent"

    def __init__(
        self,
        provider: RetrievalProvider,
        verifier: CitationVerifier,
        llm: LLMProvider | None = None,
        per_query_limit: int = 5,
        parser: StructuredParser | None = None,
        *,
        is_mock: bool = False,
    ) -> None:
        self._provider = provider
        self._verifier = verifier
        self._llm = llm
        self._limit = per_query_limit
        # 有 LLM 才有结构化解析路径；无 LLM 时保持「不调用、直接降级」语义。
        # is_mock 由解析器实例持有（#12），调用方不再每次 request_json 重复传递。
        if parser is not None:
            self._parser: StructuredParser | None = parser
        elif llm is not None:
            self._parser = StructuredParser(llm, is_mock=is_mock)
        else:
            self._parser = None
        self._is_mock = is_mock

    def run(self, ctx: AgentContext) -> AgentResult:
        ws = ctx.workspace
        topic = self._topic_of(ws)
        logs: list[str] = []

        queries = self._generate_queries(ws, topic)
        logs.append(f"检索词：{queries}")

        # 检索 + 真实性核验，汇成候选池（去重、排除已在库）。
        existing_ids = {r.id for r in ws.verified_references}
        pool: dict[str, ReferenceEntry] = {}
        for q in queries:
            try:
                candidates = self._provider.search(q, limit=self._limit)
            except RetrievalError as exc:
                logs.append(f"检索失败（{q}）：{exc}")
                continue
            for cand in candidates:
                marked = self._verifier.verify_and_mark(cand)
                if marked.verified and marked.id not in existing_ids:
                    pool[marked.id] = marked

        # 相关性过滤（第二道闸门）。
        relevant = self._filter_relevant(topic, list(pool.values()))
        logs.append(
            f"核验通过 {len(pool)} 篇，相关性过滤后保留 {len(relevant)} 篇"
        )

        def mutate(w: PaperWorkspace) -> None:
            w.verified_references.extend(relevant)

        return AgentResult(mutations=[mutate], logs=logs)

    # --- 检索词生成 ---

    def _generate_queries(self, ws: PaperWorkspace, topic: str) -> list[str]:
        if self._parser is not None:
            outcome = self._parser.request_json(
                templates.expand_queries(topic=topic),
                required_keys=("queries",),
            )
            if outcome.status is ParseStatus.PARSED and outcome.data is not None:
                qs = outcome.data.get("queries")
                if isinstance(qs, list):
                    cleaned = [str(q).strip() for q in qs if str(q).strip()]
                    if cleaned:
                        return cleaned
        # 回退（无 LLM / MOCK_FALLBACK / FAILED）：主题 + 标记需检索的任务描述。
        return self._fallback_queries(ws, topic)

    @staticmethod
    def _fallback_queries(ws: PaperWorkspace, topic: str) -> list[str]:
        queries = [topic] if topic else []
        for task in ws.task_checklist:
            if task.needs_retrieval:
                queries.append(task.description)
        return queries or ["academic paper"]

    # --- 相关性过滤 ---

    def _filter_relevant(
        self, topic: str, pool: list[ReferenceEntry]
    ) -> list[ReferenceEntry]:
        if not pool or self._parser is None:
            return pool  # 无 LLM → 不过滤（保持向后兼容）
        candidates = [(r.id, r.title) for r in pool]
        outcome = self._parser.request_json(
            templates.filter_relevant(topic=topic, candidates=candidates),
            required_keys=("relevant_ids",),
        )
        # 仅 PARSED 才据此过滤；MOCK_FALLBACK / FAILED → 全保留，避免误删（Req 3.9）。
        if outcome.status is not ParseStatus.PARSED or outcome.data is None:
            return pool
        relevant_ids = {str(i) for i in outcome.data.get("relevant_ids", [])}
        return [r for r in pool if r.id in relevant_ids]

    @staticmethod
    def _topic_of(ws: PaperWorkspace) -> str:
        if ws.topic_background:
            return ws.topic_background
        if ws.original_draft:
            return ws.original_draft[:500]
        return "academic paper"
