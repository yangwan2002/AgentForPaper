"""选择性子智能体与并行原语（Task 6）。

混合架构落地：顶层默认直接调工具；仅在"隔离即优点"处委派子智能体或并行执行。
本模块提供三块可复用能力，均**不**改变既有增量工具路径（未使用即行为不变，Property 9）：

1. :func:`run_parallel`：并发执行彼此独立的任务，单个失败被隔离、不影响其余
   （批量文献核验等无需共享上下文的场景）。
2. :class:`SubAgentRunner`：以**独立对话上下文**、**共享工作区**的有界子循环执行一个
   子目标——子智能体改工作区同样只经既有写工具 → 护栏 → 单一写路径（Property 7），
   不绕过有界性。用于"独立评审"等隔离即优点的委派。
3. :func:`build_curated_context`：为章节写作/改写提供精选上下文（全局大纲 + 术语表 +
   相邻章节摘要 + 目标章节全文），复用既有 :class:`ContextManager`，使工作单元有全局
   理解而非孤立看目标章节（Property 8）。章节写作**不做纯隔离**（共享工作区 + 精选视图）。

设计取舍：并行核验用线程池（核验是 I/O 密集的网络回查，GIL 不构成瓶颈）；子智能体
写入一致性靠"复用同一套写工具"结构性保证，而非在此另立写路径。
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from typing import Callable, TypeVar

from paper_agent.agent_platform.models import AgentSession, TaskAgentConfig
from paper_agent.agent_platform.task_agent import TaskAgent
from paper_agent.context.manager import ContextManager
from paper_agent.providers.llm.base import LLMProvider, Message
from paper_agent.tools.citation import CitationVerifier
from paper_agent.tools.registry import ToolRegistry
from paper_agent.workspace.models import PaperWorkspace, ReferenceEntry

T = TypeVar("T")


# --------------------------------------------------------------------------- #
# 并行原语
# --------------------------------------------------------------------------- #

@dataclass
class ParallelResult:
    """一个并行任务的结果（隔离：失败不抛出，记录到 ``error``）。"""

    ok: bool
    value: object = None
    error: str = ""


def run_parallel(
    tasks: list[Callable[[], T]], *, max_workers: int = 4
) -> list[ParallelResult]:
    """并发执行独立任务，返回与输入**顺序一致**的结果列表。

    单个任务抛异常被隔离为 ``ParallelResult(ok=False, error=...)``，不影响其余任务
    （Req 5.4 并行独立、互不影响）。空任务列表 → 空结果。``max_workers`` 至少 1、
    至多任务数。
    """
    if not tasks:
        return []
    workers = max(1, min(int(max_workers), len(tasks)))
    results: list[ParallelResult] = [ParallelResult(ok=False) for _ in tasks]
    with ThreadPoolExecutor(max_workers=workers) as pool:
        future_to_index = {pool.submit(task): i for i, task in enumerate(tasks)}
        for future in as_completed(future_to_index):
            index = future_to_index[future]
            try:
                results[index] = ParallelResult(ok=True, value=future.result())
            except Exception as exc:  # noqa: BLE001 - 隔离单任务失败
                results[index] = ParallelResult(ok=False, error=str(exc))
    return results


def verify_references_parallel(
    verifier: CitationVerifier,
    entries: list[ReferenceEntry],
    *,
    max_workers: int = 4,
) -> list[ReferenceEntry]:
    """并行核验一批文献，返回**带 verified 标记的新条目**（顺序与输入一致）。

    每个核验任务彼此独立（无需共享上下文），用 :func:`run_parallel` 并发。核验失败
    （网络异常等）的条目回退为"未通过核验"（``verified=False``），不影响其余。
    结果由调用方经既有单一写路径落盘（本函数不写工作区）。
    """
    tasks = [
        (lambda e=entry: verifier.verify_and_mark(e)) for entry in entries
    ]
    outcomes = run_parallel(tasks, max_workers=max_workers)
    marked: list[ReferenceEntry] = []
    for entry, outcome in zip(entries, outcomes):
        if outcome.ok and isinstance(outcome.value, ReferenceEntry):
            marked.append(outcome.value)
        else:
            # 核验失败 → 保守标记未通过（不静默当作已核验）。
            data = vars(entry).copy()
            data["verified"] = False
            marked.append(ReferenceEntry(**data))
    return marked


# --------------------------------------------------------------------------- #
# 子智能体：独立上下文 + 共享工作区的有界子循环
# --------------------------------------------------------------------------- #

@dataclass
class SubAgentResult:
    """子智能体一次运行的结构化结果。"""

    goal: str
    summary: str = ""
    delivered: bool = False        # 是否自然收尾（未触达有界上限）
    bound_hit: str | None = None   # 触达的上限类型（None 表示正常收尾）
    transcript_tail: list[dict] = field(default_factory=list)


class SubAgentRunner:
    """把一个子目标交给带**独立上下文**的有界 agent 循环执行，返回结构化结果。

    子智能体与顶层共享**同一工作区**（唯一真相源）：其对工作区的任何改动仍经传入
    ``registry`` 中的既有写工具 → 护栏 → 单一写路径（Property 7），不另立写路径、不
    绕过有界性。"独立"仅指对话消息序列独立（破自评偏置 / 隔离探索），非工作区隔离。
    """

    def __init__(self, llm: LLMProvider) -> None:
        self._llm = llm

    def run(
        self,
        session: AgentSession,
        goal: str,
        *,
        registry: ToolRegistry,
        curated_context: str = "",
        config: TaskAgentConfig | None = None,
        on_tool_call=None,
    ) -> SubAgentResult:
        agent = TaskAgent(
            self._llm,
            registry,
            config=config or TaskAgentConfig(),
            on_tool_call=on_tool_call,
        )
        messages = agent.new_conversation()
        if curated_context.strip():
            messages.append(
                Message(role="system", content="[精选上下文]\n" + curated_context)
            )
        before = len(session.transcript)
        final_content, bound_hit = agent.converse(session, messages, goal)
        return SubAgentResult(
            goal=goal,
            summary=final_content,
            delivered=bound_hit is None,
            bound_hit=bound_hit,
            transcript_tail=list(session.transcript[before:]),
        )


# --------------------------------------------------------------------------- #
# 章节写作精选上下文（共享工作区 + 精选视图，非纯隔离）
# --------------------------------------------------------------------------- #

def build_curated_context(
    ws: PaperWorkspace,
    section_id: str,
    *,
    llm: LLMProvider | None = None,
    token_budget: int = 1500,
) -> str:
    """为章节写作/改写组装精选上下文，使工作单元有全局理解（Property 8）。

    组成：全局大纲 + 论文档案 + 术语表（``ContextManager.stable_block``）+ 相邻章节
    摘要（按 token 预算裁剪）+ 目标章节全文。复用既有 ``ContextManager``（其
    ``stable_block`` / ``summaries_block`` 为纯组装，不调用 LLM，故 ``llm`` 可为
    ``None``）。
    """
    manager = ContextManager(llm, token_budget=token_budget)
    parts: list[str] = [manager.stable_block(ws)]

    summaries = manager.summaries_block(ws, section_id)
    if summaries:
        parts.append(f"[相邻章节摘要] {summaries}")

    draft = ws.section_drafts.get(section_id)
    if draft is not None:
        parts.append(f"[目标章节 {section_id}｜{draft.title}]\n{draft.content}")
    else:
        parts.append(f"[目标章节 {section_id}]（尚无草稿）")

    return "\n\n".join(parts)


__all__ = [
    "ParallelResult",
    "run_parallel",
    "verify_references_parallel",
    "SubAgentResult",
    "SubAgentRunner",
    "build_curated_context",
]
