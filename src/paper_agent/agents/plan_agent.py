"""规划智能体（Req 2）。

基于主题背景（或初稿结构）生成大纲与任务清单。
优先用 LLM 产出结构化 JSON 大纲；JSON 解析统一经 `StructuredParser` 治理
（Req 3.9）：仅当解析状态为 `PARSED` 时采用 LLM 大纲，其余情况
（Mock 回退 `MOCK_FALLBACK` 或生产失败 `FAILED`）回退到确定性的启发式大纲，
保证骨架始终可跑、不再散落静默 `extract_json` 回退。
"""

from __future__ import annotations

from paper_agent.agents.base import Agent, AgentContext, AgentResult
from paper_agent.ingestion import split_draft_into_sections
from paper_agent.parsing import StructuredParser
from paper_agent.prompts import templates
from paper_agent.providers.llm.base import LLMProvider
from paper_agent.workspace.models import (
    InputMode,
    OutlineNode,
    ParseStatus,
    PaperWorkspace,
    TaskItem,
)

# 从零生成模式的默认论文骨架章节（LLM 解析失败时的回退）。
_DEFAULT_SECTIONS = [
    ("introduction", "引言"),
    ("related_work", "相关工作"),
    ("method", "方法"),
    ("experiments", "实验"),
    ("conclusion", "结论"),
]


class PlanAgent(Agent):
    name = "plan_agent"

    def __init__(
        self,
        llm: LLMProvider,
        parser: StructuredParser | None = None,
        *,
        is_mock: bool = False,
    ) -> None:
        self._llm = llm
        # 结构化解析统一走 StructuredParser；is_mock 由解析器实例持有（#12），
        # 调用方不再每次 request_json 重复传递。默认据注入的 llm 自建，保持向后兼容。
        self._parser = (
            parser if parser is not None else StructuredParser(llm, is_mock=is_mock)
        )
        self._is_mock = is_mock

    def run(self, ctx: AgentContext) -> AgentResult:
        ws = ctx.workspace
        outline, retrieval_flags, draft_sections = self._build_outline(ws)
        tasks = self._build_tasks(outline, retrieval_flags)

        def mutate(w: PaperWorkspace) -> None:
            w.outline = outline
            w.task_checklist = tasks
            # 草稿修订模式：保留初稿各章节原文，供写作智能体作为修订基底。
            w.draft_sections = draft_sections

        return AgentResult(
            mutations=[mutate],
            logs=[f"规划完成：{len(outline)} 个章节，{len(tasks)} 个任务"],
        )

    # --- 大纲生成 ---

    def _build_outline(
        self, ws: PaperWorkspace
    ) -> tuple[list[OutlineNode], dict[str, bool], dict[str, str]]:
        """返回 (大纲, 各章节是否需检索的标记, 草稿各章节原文)。

        草稿修订模式：按初稿实际标题**确定性地**切分章节并保留各章节原文，
        而非用 LLM 另起大纲——这样章节与初稿内容对齐，写作时是「修订」而非
        「从零重写」，避免整篇初稿被压成片段后丢弃（#3 修复）。
        从零生成模式：走 LLM 大纲（解析失败回退启发式），draft_sections 为空。
        """
        if ws.input_mode is InputMode.DRAFT_REVISION and ws.original_draft:
            return self._outline_from_draft(ws)
        outline, flags = self._build_generation_outline(ws)
        return outline, flags, {}

    def _outline_from_draft(
        self, ws: PaperWorkspace
    ) -> tuple[list[OutlineNode], dict[str, bool], dict[str, str]]:
        sections = split_draft_into_sections(ws.original_draft or "")
        if not sections:
            # 初稿为空：回退通用骨架（极少见，草稿修订模式不应给空初稿）。
            outline = [
                OutlineNode(section_id=sid, title=title, order=i)
                for i, (sid, title) in enumerate(_DEFAULT_SECTIONS)
            ]
            return outline, {}, {}
        outline = [
            OutlineNode(section_id=sid, title=title, order=i)
            for i, (sid, title, _content) in enumerate(sections)
        ]
        draft_sections = {sid: content for sid, _t, content in sections}
        return outline, {}, draft_sections

    def _build_generation_outline(
        self, ws: PaperWorkspace
    ) -> tuple[list[OutlineNode], dict[str, bool]]:
        """从零生成模式：LLM 大纲，解析失败回退启发式骨架。"""
        llm_result = self._try_llm_outline(ws)
        if llm_result is not None:
            return llm_result
        return self._fallback_outline(ws)

    def _try_llm_outline(
        self, ws: PaperWorkspace
    ) -> tuple[list[OutlineNode], dict[str, bool]] | None:
        """经 StructuredParser 生成 JSON 大纲；非 PARSED 则返回 None 以回退。"""
        draft_excerpt = (ws.original_draft or "")[:500]
        messages = templates.plan_outline(
            topic_background=ws.topic_background or "",
            input_mode=ws.input_mode.value,
            draft_excerpt=draft_excerpt,
        )
        outcome = self._parser.request_json(
            messages, required_keys=("sections",)
        )
        # 仅 PARSED 采用 LLM 大纲；MOCK_FALLBACK / FAILED 回退启发式（Req 3.9）。
        if outcome.status is not ParseStatus.PARSED or outcome.data is None:
            return None
        sections = outcome.data.get("sections")
        if not isinstance(sections, list) or not sections:
            return None

        outline: list[OutlineNode] = []
        flags: dict[str, bool] = {}
        for i, sec in enumerate(sections):
            if not isinstance(sec, dict):
                continue
            sid = str(sec.get("section_id") or f"sec_{i}")
            title = str(sec.get("title") or sid)
            outline.append(
                OutlineNode(
                    section_id=sid,
                    title=title,
                    order=i,
                    summary_hint=str(sec.get("summary_hint", "")),
                )
            )
            flags[sid] = bool(sec.get("needs_retrieval", False))
        return (outline, flags) if outline else None

    def _fallback_outline(
        self, ws: PaperWorkspace
    ) -> tuple[list[OutlineNode], dict[str, bool]]:
        """从零生成模式下 LLM 大纲解析失败的回退：通用骨架。"""
        outline = [
            OutlineNode(section_id=sid, title=title, order=i)
            for i, (sid, title) in enumerate(_DEFAULT_SECTIONS)
        ]
        return outline, {}

    # --- 任务清单 ---

    @staticmethod
    def _build_tasks(
        outline: list[OutlineNode], retrieval_flags: dict[str, bool]
    ) -> list[TaskItem]:
        tasks: list[TaskItem] = []
        for node in outline:
            # 优先用 LLM 给出的检索标记；缺省回退到启发式（相关工作类章节需检索）。
            if node.section_id in retrieval_flags:
                needs_retrieval = retrieval_flags[node.section_id]
            else:
                needs_retrieval = node.section_id in {"related_work"} or (
                    "相关" in node.title
                )
            tasks.append(
                TaskItem(
                    id=f"task_{node.section_id}",
                    description=f"撰写章节：{node.title}",
                    section_ref=node.section_id,
                    needs_retrieval=needs_retrieval,
                )
            )
        return tasks
