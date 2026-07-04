"""术语抽取智能体（Round 8：主动构建术语表，供语言润色统一用词）。

此前 ``ws.glossary`` 只能靠外部注入——若用户不提供，语言润色就没有「统一术语」
依据，全篇用词不一致（缩写/大小写/中英混用）无法被系统性纠正。本智能体在语言
润色**之前**运行一次：读全文，让 LLM 抽取核心术语及其规范写法，写入 ``ws.glossary``
（不覆盖用户已提供的条目），随后语言润色据此对齐用词。

契约：
- 结构化输出经 ``StructuredParser`` 治理——仅 ``PARSED`` 才采用；Mock/失败 → no-op
  （不改工作区，逐字节不变），保证既有基于 Mock 的测试与「停用等价」语义；
- 单一写入路径：经 ``AgentResult.mutations`` → ``WorkspaceRepository`` 落盘；
- 只做 ``setdefault``：不覆盖用户已注入的术语规范。
"""

from __future__ import annotations

from paper_agent.agents.base import Agent, AgentContext, AgentResult
from paper_agent.parsing.structured_parser import StructuredParser
from paper_agent.prompts import templates
from paper_agent.providers.llm.base import LLMProvider
from paper_agent.workspace.models import ParseStatus, PaperWorkspace
from paper_agent.workspace.paper_view import assemble_paper_text

# 送入术语抽取的正文字符上限（防长论文撑爆上下文/费用）。
_MAX_CHARS = 20000


class TerminologyAgent(Agent):
    name = "terminology_agent"

    def __init__(
        self,
        llm: LLMProvider,
        *,
        parser: StructuredParser | None = None,
        is_mock: bool = False,
        max_terms: int = 15,
    ) -> None:
        self._parser = parser or StructuredParser(llm, is_mock=is_mock)
        self._is_mock = is_mock
        self._max_terms = max(1, int(max_terms))

    def run(self, ctx: AgentContext) -> AgentResult:
        ws = ctx.workspace
        # Mock/测试 provider：no-op（逐字节不变）。
        if self._is_mock or not ws.section_drafts:
            return AgentResult()

        paper_text = assemble_paper_text(ws)[:_MAX_CHARS]
        if not paper_text.strip():
            return AgentResult()

        outcome = self._parser.request_json(
            templates.extract_terminology(
                paper_text=paper_text, max_terms=self._max_terms
            ),
            required_keys=("terms",),
        )
        if outcome.status is not ParseStatus.PARSED or not outcome.data:
            return AgentResult(logs=["术语抽取：无可信输出，跳过"])

        raw = outcome.data.get("terms")
        if not isinstance(raw, list):
            return AgentResult(logs=["术语抽取：terms 非列表，跳过"])

        new_terms: dict[str, str] = {}
        for item in raw[: self._max_terms]:
            if not isinstance(item, dict):
                continue
            term = str(item.get("term", "")).strip()
            if not term:
                continue
            new_terms[term] = str(item.get("definition", "")).strip()

        if not new_terms:
            return AgentResult(logs=["术语抽取：未抽到术语"])

        def mutate(w: PaperWorkspace) -> None:
            for term, definition in new_terms.items():
                # 不覆盖用户已提供的术语规范（setdefault）。
                w.glossary.setdefault(term, definition)

        return AgentResult(
            mutations=[mutate],
            logs=[f"术语抽取：入库 {len(new_terms)} 个核心术语（供润色统一用词）"],
        )


__all__ = ["TerminologyAgent"]
