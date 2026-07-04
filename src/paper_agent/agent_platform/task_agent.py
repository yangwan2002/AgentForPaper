"""TaskAgent：顶层有界工具循环（Agent_Loop）。

平台的「大脑」。以用户的自然语言 ``Writing_Task`` 为目标，让 LLM 面对已注册的全部
工具自主编排、多步推进，直至给出最终答复或触达有界上限。相比 ``agents/tool_loop``
的子循环，本顶层循环额外补齐两闸——墙钟超时与全局 token 预算（Req 9.1/9.2），并
复用其历史压缩（``compact_history``）与结果截断（``truncate_to_tokens``），不重复
实现 token 逻辑（Req 10.2/10.4）。

行为约束经系统提示注入（Req 8）：目标不清→调 ``ask_user`` 澄清；超出能力→如实说
无法完成；部分完成→说明已完成/未完成。护栏由写工具内部经单一写路径强制，
Agent_Loop 无法绕过（设计 Property 1）。
"""

from __future__ import annotations

import time

from paper_agent.agent_platform.bounds import budget_exceeded, deadline_exceeded
from paper_agent.agent_platform.models import (
    AgentSession,
    TaskAgentConfig,
    TaskResult,
)
from paper_agent.agents.tool_loop import compact_history, truncate_to_tokens
from paper_agent.agents.tool_loop import ToolLoopConfig
from paper_agent.context.tokenizer import TokenCounter, build_token_counter
from paper_agent.observability.events import EventKind, EventSink, NullSink
from paper_agent.observability.tracing import new_trace, span
from paper_agent.observability.usage import UsageTracker
from paper_agent.providers.llm.base import LLMProvider, Message
from paper_agent.tools.registry import ToolRegistry

_SYSTEM_PROMPT = (
    "你是一名学术论文写作智能体。用户会用自然语言下达写作任务，你需要调用可用工具"
    "自主完成它。行为准则：\n"
    "1. 先理解任务意图；不清楚或有多种同等可能的解读时，调用 ask_user 向作者澄清，"
    "不要擅自臆断。\n"
    "2. 定位章节用 locate_section；改写/润色前先用 read_section 看现状。\n"
    "3. 改动论文只能通过提供的写工具（rewrite_section/polish_section/"
    "edit_section_anchor/add_references 等）。系统会对每次改动做学术正确性把关，"
    "未通过会返回原因，请据原因修正后重试，切勿编造事实、数字或引用。\n"
    "3.1 引用规则：只能引用「已验证文献库」中的文献，正文引用写作 [编号]。要引用原文"
    "已列出的文献，先调 verify_existing_references 核验入库；要新文献用 add_references"
    "检索——它会**返回每篇可引用的编号**，你必须**严格使用返回的编号**在正文标注 [编号]，"
    "不要自己臆造或从 1 重新编号，否则会被判为未核验引用而无法落盘。\n"
    "3.2 正文引用**只写方括号编号**（如 [7]、多篇写 [7][10]）。**绝对不要**在正文里写 "
    "LaTeX 原生命令 \\cite{}、\\ref{}、\\label{} 等——导出为 LaTeX 时系统会自动把 [编号] "
    "转成 \\cite{}；你若自己写 \\cite{...}，其花括号会在导出时被转义破坏、导致 .tex 无法"
    "编译。同理正文正文体裁按 Markdown 写，不要手写 LaTeX 排版命令。\n"
    "4. 整篇从零撰写或整篇重渲染这类重任务，使用 run_full_pipeline。\n"
    "4.1 保格式红线：当用户提供的是 .docx 且诉求是「保留原格式做润色 / 调排版（两端"
    "对齐、行距、首行缩进等）」时，必须用 polish_docx_inplace（就地处理原 docx、保留"
    "字体/样式/编号/页眉页脚/图/表/公式等一切格式）；当用户提供的是 .tex 且要「保留原"
    "结构做语言润色」时，必须用 polish_latex_inplace（就地润色原 tex、保留 preamble/宏/"
    "公式/引用/图表）。绝不要用 import_draft + rewrite/polish_section + export_paper 那条"
    "路做「保格式润色」——它会从文本重建 docx/tex，导致用户原排版、宏与图表丢失。只有当"
    "用户明确要「重写内容/从文本重新生成/新增引用」时才走重建路径。\n"
    "4.2 跨格式转换红线：当用户要「把 X 格式转成 Y 格式」（如 .tex 转 docx、.docx 转 "
    "latex）时，必须用 convert_document（pandoc 直转，公式转成原生公式、章节结构保留，"
    "docx 还能设双栏）。**绝不要**用 import_draft + set_typesetting + export_paper 去做"
    "格式转换——那条路把 LaTeX 当纯文本重建，会导致公式变成裸符号、结构错乱。\n"
    "5. 若任务超出可用工具的能力范围，如实告诉用户无法完成及原因，不要假装完成。\n"
    "6. 完成后，用简洁自然语言总结你实际做了什么；若只完成了一部分，明确说明已完成"
    "与未完成的部分。\n"
    "7. 学术诚信红线（不可逾越）：绝不编造或篡改参考文献的作者姓名、标题、年份等信息，"
    "也不得编造数据、实验结果或引用。若某条参考文献因显示截断等原因你无法看全，"
    "就保持其原样不动、或向用户说明，切勿凭记忆臆造或用占位内容填充。遇到工具报错时，"
    "先如实向用户说明错误，不要通过删改或捏造文献内容来'绕过'报错。\n"
    "8. 评审是按需能力：只有用户明确要求「评审/审阅/看看写得怎么样」时才调用 review_paper；"
    "排版、定点编辑、导出等具体任务不要顺带评审。简单或局部的操作直接用相应工具完成，"
    "不必为每个改动都追加一次自我评审。\n"
    "9. 严格听从用户的最新指令——它**优先于**先前的目标或数量要求。当用户明确叫停某类"
    "操作（如「别搜了」「不搜了」「直接写」「就用现有的」），你必须**立即停止该操作**"
    "（如不再调用 add_references），改用现有材料继续完成任务，绝不因为之前定过「凑够 N "
    "篇」之类的目标而继续。另外：同一工具**连续失败**（如 add_references 被限流返回 429/"
    "检索失败）时，不要一遍遍重试——如实告诉用户该能力暂时不可用，并用现有材料推进。\n"
    "10. 严守任务边界（非常重要）：**只做用户明确要求的事**。用户要求「转格式」，你就只做"
    "格式转换；用户要求「润色某章」，你就只改那一章。完成被要求的任务后**立即停止并交回"
    "用户**，用简洁总结说明做了什么。**绝不主动扩展**去做没被要求的工作——例如补写缺失"
    "章节、排查/修正悬空引用、优化其它章节、检索文献等；这些哪怕你觉得有用，也**必须先"
    "用 ask_user 征询用户是否要做**，得到明确同意后才做。\n"
    "11. 提问即停：当你需要用户拍板（如「要不要补写缺失章节」「是否继续」「用哪种格式」），"
    "**必须调用 ask_user 工具提问，然后停止调用任何其它工具、结束本轮**，等用户回答后再"
    "行动。绝不允许「一边抛出问题、一边自己继续做下去」——那等于没问。只在纯文本总结里"
    "写「需要我做吗？」而不停下，是错误的。"
)


class TaskAgent:
    """顶层任务智能体。"""

    def __init__(
        self,
        llm: LLMProvider,
        registry: ToolRegistry,
        *,
        config: TaskAgentConfig | None = None,
        counter: TokenCounter | None = None,
        tracker: UsageTracker | None = None,
        deadline_s: float = 0.0,
        token_budget: int = 0,
        sink: EventSink | None = None,
        on_tool_call=None,
        acceptance_finalizer=None,
        stop_after_delivery: bool = True,
    ) -> None:
        self._llm = llm
        self._registry = registry
        self._config = config or TaskAgentConfig()
        self._counter = counter or build_token_counter()
        self._tracker = tracker
        self._deadline_s = deadline_s
        self._token_budget = token_budget
        self._sink = sink or NullSink()
        # 可选回调：每次工具调用前触发 on_tool_call(name, args)，供 CLI 实时展示。
        self._on_tool_call = on_tool_call
        # 可选收尾器：finalizer(agent, session, messages, result) -> result。
        # None 时行为不变（Property 9 向后兼容）；由装配层接入确定性验收 + 有界自愈。
        self._acceptance_finalizer = acceptance_finalizer
        # 机制级 harness「交付即停」：本轮成功产出交付物（导出/转换/保结构润色等产出
        # 文件）后，强制收尾、交回用户，不再继续调工具做未被要求的扩展工作（如转完
        # 格式又擅自去排查引用）。默认开；关掉恢复"仅靠提示自觉"的旧行为。
        self._stop_after_delivery = stop_after_delivery

    def run(self, session: AgentSession) -> TaskResult:
        """一次性任务：以任务描述为首条用户消息跑完循环，返回 TaskResult。"""
        # 整次运行归入一条 trace（该运行内所有事件共享同一 trace_id）。
        with new_trace():
            messages = self.new_conversation()
            messages.append(Message(role="user", content=session.task.instruction))
            final_content, bound_hit = self._run_loop(session, messages)
            result = self._build_result(session, final_content, bound_hit)
            return self._finalize(session, messages, result)

    def _finalize(
        self, session: AgentSession, messages: list[Message], result: TaskResult
    ) -> TaskResult:
        """收尾钩子：若接入了验收收尾器则运行之（失败不影响主结果）。"""
        if self._acceptance_finalizer is None:
            return result
        try:
            return self._acceptance_finalizer(self, session, messages, result)
        except Exception as exc:  # noqa: BLE001 - 验收收尾异常不吞没主结果
            self._emit(f"验收收尾异常（已跳过）：{exc}")
            return result

    def new_conversation(self) -> list[Message]:
        """构造一段新对话的初始消息（仅含系统提示），供多轮对话复用。"""
        return [Message(role="system", content=_SYSTEM_PROMPT)]

    def converse(
        self, session: AgentSession, messages: list[Message], user_text: str
    ) -> tuple[str, str | None]:
        """多轮对话的一轮：把 user_text 追加到持久 messages 后跑一轮循环。

        返回 (助手最终答复, 触达的上限或 None)。``messages`` 被原地更新（含本轮的
        assistant/tool 消息与最终答复），供下一轮延续上下文。
        """
        messages.append(Message(role="user", content=user_text))
        return self._run_loop(session, messages)

    def _run_loop(
        self, session: AgentSession, messages: list[Message]
    ) -> tuple[str, str | None]:
        """在给定消息列表上运行有界工具循环，返回 (最终答复, bound_hit)。

        循环结束都会把最终答复作为一条 assistant 消息追加进 ``messages``，以保证
        多轮对话的上下文连续性。每次调用重置墙钟窗口（deadline 为单轮限制）。
        """
        schemas = self._registry.to_openai_schemas()
        loop_cfg = self._loop_config()
        start = time.monotonic()

        final_content = ""
        bound_hit: str | None = None

        for _ in range(self._config.max_iters):
            bound_hit = self._check_bounds(start)
            if bound_hit is not None:
                final_content = self._force_finish(messages)
                break

            messages[:] = self._compact_if_needed(messages, loop_cfg)
            resp = self._llm.complete(messages, tools=schemas)

            if not resp.tool_calls:
                final_content = resp.content or ""
                bound_hit = None
                break

            messages.append(
                Message(role="assistant", content=resp.content or "", tool_calls=resp.tool_calls)
            )
            before = len(session.transcript)
            for call in resp.tool_calls:
                self._execute_tool(session, messages, call)
            # 交付即停（机制级 harness）：本轮成功产出交付物后强制收尾、交回用户，
            # 不再进入下一轮工具调用做未被要求的扩展工作。
            if self._stop_after_delivery and _delivered_this_turn(
                session, before, resp.tool_calls
            ):
                final_content = self._force_finish(messages)
                bound_hit = None
                break
        else:
            # 用满 max_iters 仍未自然收尾 → 去掉工具强制收尾（Req 9.2）。
            bound_hit = "max_iters"
            final_content = self._force_finish(messages)

        # 追加最终答复，保证下一轮对话能看到本轮结论。
        if final_content:
            messages.append(Message(role="assistant", content=final_content))
        return final_content, bound_hit

    def _emit(self, message: str) -> None:
        """发一条可观测日志事件（sink 异常不影响主流程）。"""
        from paper_agent.observability.events import Event

        try:
            self._sink.emit(Event(kind=EventKind.AGENT_LOG, message=message))
        except Exception:  # noqa: BLE001
            pass

    # --- 循环内部 -----------------------------------------------------------

    def _loop_config(self) -> ToolLoopConfig:
        return ToolLoopConfig(
            max_iters=self._config.max_iters,
            context_token_budget=self._config.context_token_budget,
            max_tool_result_tokens=self._config.max_tool_result_tokens,
            keep_recent_turns=self._config.keep_recent_turns,
        )

    def _check_bounds(self, start: float) -> str | None:
        """返回触达的上限类型；未触达返回 None（Req 9.2）。"""
        if deadline_exceeded(start, self._deadline_s):
            return "deadline"
        if self._tracker is not None and budget_exceeded(
            self._tracker.total_tokens, self._token_budget
        ):
            return "token_budget"
        return None

    def _compact_if_needed(self, messages, loop_cfg):
        if self._counter.count_messages(messages) <= loop_cfg.context_token_budget:
            return messages
        return compact_history(messages, self._counter, loop_cfg, self._summarize)

    def _summarize(self, text: str) -> str:
        """历史压缩摘要（失败回退截断，绝不中断循环）。"""
        try:
            resp = self._llm.complete(
                [
                    Message(role="system", content="把以下对话压缩为要点，保留已完成与待办。只输出要点。"),
                    Message(role="user", content=text),
                ]
            )
            return (resp.content or "").strip() or text[:2000]
        except Exception:  # noqa: BLE001
            return text[:2000]

    def _execute_tool(self, session: AgentSession, messages: list, call) -> None:
        """执行一次工具调用，把（截断后的）结果回灌为 tool 消息（Req 2.3/2.4/10.2）。"""
        if self._on_tool_call is not None:
            try:
                self._on_tool_call(call.name, call.arguments)
            except Exception:  # noqa: BLE001 - 展示回调异常不影响主流程
                pass
        # 每次工具调用作为一个 span（带耗时，parent 为当前 LLM turn/trace 根）。
        with span(self._sink, f"tool.{call.name}", data={"tool": call.name}):
            try:
                result = self._registry.call(call.name, **call.arguments)
                result_text = str(result)
            except Exception as exc:  # noqa: BLE001 - 工具失败回灌供自纠，不中止（Req 2.4）
                result_text = f"工具执行失败：{exc}"
        session.record("tool_call", name=call.name, args=call.arguments)

        note = "\n\n[结果过长已截断]"
        result_text = truncate_to_tokens(
            result_text, self._config.max_tool_result_tokens, self._counter, note=note
        )
        messages.append(Message(role="tool", content=result_text, tool_call_id=call.id))

    def _force_finish(self, messages: list) -> str:
        """去掉工具，让 LLM 基于现有上下文给出最终收尾答复。"""
        try:
            resp = self._llm.complete(messages)
            return resp.content or ""
        except Exception:  # noqa: BLE001
            return "任务已停止。"

    def _build_result(
        self, session: AgentSession, final_content: str, bound_hit: str | None
    ) -> TaskResult:
        guardrail_report = _aggregate_guardrail(session.transcript)
        export_files = _collect_export_files(session.transcript)
        if bound_hit is not None:
            self._emit(f"任务触达上限：{bound_hit}")
        return TaskResult(
            session_id=session.session_id,
            summary=final_content,
            guardrail_report=guardrail_report,
            bound_hit=bound_hit,
            export_files=export_files,
        )


# --------------------------------------------------------------------------- #
# 辅助
# --------------------------------------------------------------------------- #

# 交付类工具：成功调用即产出最终交付物（文件），是任务的终点性动作。产出后应交回
# 用户，而非继续扩展做未被要求的工作（机制级 harness「交付即停」）。
_TERMINAL_TOOLS = frozenset(
    {
        "convert_document",
        "export_paper",
        "polish_docx_inplace",
        "polish_latex_inplace",
        "run_full_pipeline",
    }
)


def _delivered_this_turn(session: AgentSession, before_len: int, tool_calls) -> bool:
    """本轮是否**成功**产出交付物：调用了交付类工具，且其在 transcript 里记录了产出文件。

    仅当交付类工具成功（record 带非空 ``files``）才算交付——失败（如 pandoc 不可用、
    无产出）不触发停止，留给 agent 重试或改走其它路径。
    """
    if not any(getattr(c, "name", "") in _TERMINAL_TOOLS for c in tool_calls):
        return False
    for entry in session.transcript[before_len:]:
        if entry.get("files"):
            return True
    return False


def _aggregate_guardrail(transcript: list[dict]) -> dict:
    """从 transcript 汇总护栏结果（被拒次数、通过次数）。"""
    passed = sum(1 for e in transcript if e.get("passed") is True)
    rejected = sum(int(e.get("rejected", 0) or 0) for e in transcript)
    return {"changes_passed": passed, "changes_rejected": rejected}


def _collect_export_files(transcript: list[dict]) -> list[str]:
    """从 transcript 收集导出/管线产出的文件路径（去重保序）。"""
    files: list[str] = []
    for e in transcript:
        for f in e.get("files", []) or []:
            if f not in files:
                files.append(f)
    return files


__all__ = ["TaskAgent"]
