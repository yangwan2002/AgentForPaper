"""交互式对话层（路径 A）：把一次性任务升级为多轮会话（Claude Code 式 CLI）。

- ``ChatController``：持有一段跨轮存活的对话（messages）、会话与工具注册表，
  每轮 ``send(user_text)`` 追加一条用户消息并跑一轮有界工具循环，返回助手答复。
  每轮后持久化会话（transcript / 问答），支持中断续跑。
- ``run_chat_repl``：一个极简终端 REPL，读一行→发一轮→打印，支持 ``/exit`` 等命令，
  并实时展示 agent 的工具调用（Claude Code 那种"看得见它在干活"的手感）。

对话连续性由 ``TaskAgent.converse`` 在同一 messages 列表上延续实现；澄清（ask_user）
在交互模式下经 ``CLIElicitor`` 直接读终端输入，与 REPL 的输入顺序天然兼容。
"""

from __future__ import annotations

import sys
from dataclasses import dataclass, field
from typing import Callable

from paper_agent.agent_platform.models import AgentSession
from paper_agent.agent_platform.routing import (
    ConfirmOutcome,
    Intent,
    IntentRouter,
    confirm_intent,
)
from paper_agent.agent_platform.session_store import save_session
from paper_agent.agent_platform.task_agent import TaskAgent
from paper_agent.agent_platform.tools.context import ToolContext
from paper_agent.agent_platform.workflows.base import Workflow
from paper_agent.observability.events import Event, EventKind
from paper_agent.observability.tracing import new_trace
from paper_agent.providers.llm.base import Message


class StreamingChatSink:
    """极简流式 sink：只把 LLM 的**内容**增量实时写到终端，其余事件忽略。

    复用既有可观测管道（``ObservableLLMProvider`` 会把 provider 的 on_delta 增量
    转成 ``LLM_DELTA`` 事件）。装配时把本 sink 传给 ``build_agent_app`` 即可让回答
    边生成边逐字显示；请求预览 / 用量等噪音事件一律不渲染，保持对话干净。
    """

    def __init__(self, write: Callable[[str], None] | None = None) -> None:
        self._write = write or self._default_write

    @staticmethod
    def _default_write(text: str) -> None:
        sys.stdout.write(text)
        sys.stdout.flush()

    def emit(self, event: Event) -> None:
        if event.kind is EventKind.LLM_DELTA:
            data = event.data or {}
            if data.get("kind") == "content" and event.message:
                self._write(event.message)


@dataclass
class ChatTurn:
    """一轮对话的结果。"""

    reply: str
    bound_hit: str | None = None
    tool_calls: list[str] = field(default_factory=list)
    # 本轮是否有实质推进（有改动通过护栏落盘）——供自动续跑判断是否停滞。
    made_progress: bool = False
    # 收尾验收提示（本轮产生导出产物时对成品跑确定性验收的结论），供 REPL 展示。
    acceptance_note: str = ""
    # 本轮回复是否已由 LLM **实时流式**打印（仅 converse 路径为真）。工作流/取消路径的
    # 回复不经流式产生，REPL 在流式模式下须显式打印，否则结果会被"吞掉"（看似任务终止）。
    streamed: bool = False


class ChatController:
    """跨轮存活的对话控制器。"""

    def __init__(
        self,
        agent: TaskAgent,
        session: AgentSession,
        repo,
        ask_tool=None,
        *,
        output_dir: str = "output",
        enable_acceptance: bool = True,
        acceptance_max_heal_rounds: int = 1,
        router: IntentRouter | None = None,
        workflows: dict[Intent, Workflow] | None = None,
        tool_context: ToolContext | None = None,
        routing_enabled: bool = False,
        confirm_threshold: float = 0.75,
        visual_gate=None,
        visual_enabled: bool = False,
        visual_max_rounds: int = 1,
        visual_dpi: int = 150,
        visual_max_pages: int = 6,
    ) -> None:
        self._agent = agent
        self._session = session
        self._repo = repo
        self._ask_tool = ask_tool
        self._messages: list[Message] = agent.new_conversation()
        # 视觉版面验收闸（visual-layout-acceptance）：默认关；开启且注入 gate 时，收尾在
        # 本轮含版面操作 / agent 主动调 check_layout 时渲染产物看图判断、有界重改、诚实上报。
        self._visual_gate = visual_gate
        self._visual_enabled = bool(visual_enabled and visual_gate is not None)
        self._visual_max_rounds = visual_max_rounds
        self._visual_dpi = visual_dpi
        self._visual_max_pages = visual_max_pages
        # 收尾验收：本轮产生导出产物时，对成品跑一次确定性验收 + 有界自愈（P0）。
        self._output_dir = output_dir
        self._enable_acceptance = enable_acceptance
        self._acceptance_max_heal_rounds = acceptance_max_heal_rounds
        # 意图路由 + 确定性工作流（intent-routing-and-workflows）：三者齐备且开关开启
        # 才启用；否则每轮全部走既有 converse（向后兼容 Property 8）。
        self._router = router
        self._workflows = workflows or {}
        self._tool_context = tool_context
        self._confirm_threshold = confirm_threshold
        self._routing_enabled = bool(
            routing_enabled and router is not None and tool_context is not None
        )

    @property
    def session(self) -> AgentSession:
        return self._session

    def send(self, user_text: str) -> ChatTurn:
        """发送一轮用户消息，跑一轮工具循环，返回助手答复。"""
        # 每一轮对话归入一条 trace（本轮所有事件共享同一 trace_id）。
        with new_trace():
            return self._send_traced(user_text)

    def _send_traced(self, user_text: str) -> ChatTurn:
        # 意图路由前置：命中固定任务 → 确认后走确定性工作流，不进 TaskAgent。
        # 开放任务 / 未启用 / 无对应工作流 → 落既有 converse（行为不变）。
        if self._routing_enabled:
            routed = self._try_route(user_text)
            if routed is not None:
                return routed

        before = len(self._session.transcript)
        reply, bound = self._agent.converse(self._session, self._messages, user_text)

        # 本轮新增的 transcript 条目。
        new_entries = self._session.transcript[before:]
        tool_calls = [
            e.get("name", "") for e in new_entries if e.get("kind") == "tool_call"
        ]
        # 是否有实质推进：本轮存在通过护栏落盘的改动（passed=True）。
        made_progress = any(e.get("passed") is True for e in new_entries)

        # 收尾验收（P0）：仅当本轮产生了导出产物时，对成品跑一次确定性验收 + 有界
        # 自愈，把乱码/排版未应用/悬空引用/数量年限等未达标项如实附加到回复。
        acceptance_note = self._maybe_run_acceptance(user_text, new_entries)
        acceptance_note = self._append_visual_note(acceptance_note, user_text, new_entries)

        self._persist()
        return ChatTurn(
            reply=reply,
            bound_hit=bound,
            tool_calls=tool_calls,
            made_progress=made_progress,
            acceptance_note=acceptance_note,
            streamed=True,  # converse 路径的回复已由 LLM 实时流式打印
        )

    def _try_route(self, user_text: str) -> ChatTurn | None:
        """意图路由 + 确认 + 确定性工作流分流。

        返回 ``ChatTurn`` 表示本轮已由工作流处理（不再进 TaskAgent）；返回 ``None``
        表示应回落既有 ``converse``（开放任务 / 用户改选开放 / 无对应工作流）。
        路由/确认任何异常都回落 None，绝不因路由失败拒绝服务。
        """
        try:
            decision = self._router.route(user_text, self._session.workspace)
        except Exception:  # noqa: BLE001 - 路由失败 → 回落既有路径
            return None
        if decision.intent not in Intent.fixed_tasks():
            return None  # 开放任务：走既有自由智能体

        outcome = confirm_intent(
            decision, self._tool_context.elicitor, threshold=self._confirm_threshold
        )
        if not outcome.proceed:
            # 用户取消：不执行任何工作流，把说明交回用户（问前不动手，Property 3/9）。
            return ChatTurn(reply=outcome.message or "已取消。")
        if outcome.intent not in Intent.fixed_tasks():
            return None  # 用户改选「按开放处理」：回落既有自由智能体

        workflow = self._workflows.get(outcome.intent)
        if workflow is None:
            return None  # 无对应工作流：保守回落既有路径

        return self._run_workflow(workflow, outcome, user_text)

    def _run_workflow(
        self, workflow: Workflow, outcome: ConfirmOutcome, user_text: str
    ) -> ChatTurn:
        """执行确定性工作流并渲染为 ChatTurn（含产物的收尾验收）。

        **确定性核心 + 柔性长尾兜底**：工作流只保证它写死的那部分（如格式转换 + 双栏 +
        三线表）；核心覆盖不了的排版细项（字体/字号、图跨栏）作为 ``followups`` 转交自由
        智能体（open→run_python / python-docx）在产物上完成——绝不静默丢弃、绝不谎报完成。
        """
        before = len(self._session.transcript)
        result = workflow.run(self._tool_context, outcome.params)
        reply = result.message()
        tool_calls = [outcome.intent.value]

        # 兜底转交：确定性转换成功且有产物、且存在核心未覆盖的排版细项 → 交柔性通道完成。
        if result.ok and result.files and result.followups:
            spill_reply, spill_tools = self._spill_to_agent(
                produced=result.files[0],
                followups=result.followups,
                source_text=str(outcome.params.get("followup_source_text") or user_text),
            )
            if spill_reply:
                reply = (reply + "\n\n" + spill_reply).strip()
            tool_calls += spill_tools

        new_entries = self._session.transcript[before:]
        made_progress = result.ok or any(e.get("passed") is True for e in new_entries)
        acceptance_note = self._maybe_run_acceptance(user_text, new_entries)
        acceptance_note = self._append_visual_note(acceptance_note, user_text, new_entries)
        self._persist()
        return ChatTurn(
            reply=reply,
            tool_calls=tool_calls,
            made_progress=made_progress,
            acceptance_note=acceptance_note,
        )

    def _spill_to_agent(
        self, produced: str, followups: list[str], source_text: str
    ) -> tuple[str, list[str]]:
        """把转换核心未覆盖的排版细项转交自由智能体，在产物上完成（best-effort）。

        复用既有 ``converse`` 路径（同一 messages 延续），指令明确"只改排版、不动正文/
        公式/结构，走副本 + 结构无损校验"。返回 (智能体回复, 本次工具名列表)。
        任何异常都吞掉并如实降级——转交失败不该拖垮已成功的转换产物。
        """
        pending = "\n".join(f"- {item}" for item in followups)
        instruction = (
            "（系统转交）确定性转换已完成：源文件已转为 docx，并按需设置了整篇双栏 / 三线表 / "
            f"表格列宽。产物路径：{produced}\n"
            "以下用户排版诉求转换核心未覆盖，请**在该产物文件上**用 run_python + python-docx "
            "完成（务必走副本并做结构无损校验；只改排版，不改正文/公式/结构；完成后覆盖回该文件"
            "或另存并告知最终路径）：\n"
            f"{pending}\n"
            f"用户原话：{source_text}\n"
            "已完成的转换/双栏/三线表不要重复做。若某项确实做不到，请如实说明，不要假装完成。"
        )
        try:
            before = len(self._session.transcript)
            reply, _bound = self._agent.converse(
                self._session, self._messages, instruction
            )
            new_entries = self._session.transcript[before:]
            tools = [
                e.get("name", "") for e in new_entries if e.get("kind") == "tool_call"
            ]
            return reply or "", tools
        except Exception:  # noqa: BLE001 - 转交失败不影响已成功的转换产物
            return (
                "（上述排版细项自动转交处理时出错，你可以再说一句让我用 run_python 单独处理。）",
                [],
            )

    def _append_visual_note(self, note: str, user_text: str, new_entries: list[dict]) -> str:
        """把视觉验收结论（若触发）拼到收尾提示后（诚实上报，未触发则不改）。"""
        visual = self._maybe_visual_accept(user_text, new_entries)
        return "\n".join(x for x in (note, visual) if x)

    def _maybe_visual_accept(self, user_text: str, new_entries: list[dict]) -> str:
        """本轮若含版面操作 / agent 主动调 check_layout，则对 docx 产物跑视觉验收闸。

        默认关（未启用 / 未注入 gate → 空串）。渲染看图判断版面是否符合用户诉求，
        不满足则经 ``heal_fn`` 让编辑智能体在同一对话上有界重改（走既有写路径）。
        任何异常都吞并降级为空串——绝不拖垮主对话。
        """
        if not self._visual_enabled or self._visual_gate is None:
            return ""
        produced = ""
        for e in new_entries:
            for f in (e.get("files") or []):
                if str(f).lower().endswith(".docx"):
                    produced = str(f)
        if not produced:
            return ""

        from paper_agent.agent_platform.visual.triggers import touched_layout

        agent_requested = any(e.get("name") == "check_layout" for e in new_entries)
        if not (touched_layout(new_entries) or agent_requested):
            return ""

        profile = getattr(self._session.workspace, "profile", None) or {}
        src = str(profile.get("source_document_path", "") or "")
        baseline = src if (src.lower().endswith(".docx") and src != produced) else None

        def heal_fn(defects: list[str]) -> None:
            pending = "\n".join(f"- {d}" for d in defects)
            prompt = (
                "（系统视觉校验）渲染产物后发现以下版面问题，请在该 docx 产物上用 "
                f"run_python + python-docx 修正（走副本 + 结构无损校验，只改排版、不动正文/"
                f"公式/结构）：\n产物：{produced}\n{pending}"
            )
            try:
                self._agent.converse(self._session, self._messages, prompt)
            except Exception:  # noqa: BLE001 - 重改失败不拖垮主流程
                pass

        try:
            outcome = self._visual_gate.evaluate(
                produced, user_text, baseline_docx=baseline, heal_fn=heal_fn,
                max_rounds=self._visual_max_rounds, dpi=self._visual_dpi,
                max_pages=self._visual_max_pages,
            )
        except Exception:  # noqa: BLE001 - 闸门异常 → 静默降级
            return ""
        try:
            self._session.record(
                "visual_acceptance", ran=outcome.ran, satisfied=outcome.satisfied,
                defects=list(outcome.defects), rounds=outcome.rounds, backend=outcome.backend,
            )
        except Exception:  # noqa: BLE001
            pass
        return outcome.message()

    def _maybe_run_acceptance(self, user_text: str, new_entries: list[dict]) -> str:
        """本轮若产出导出文件则跑收尾验收，返回给用户的提示（否则空串）。"""
        if not self._enable_acceptance:
            return ""
        produced_files = any(e.get("files") for e in new_entries)
        if not produced_files:
            return ""
        from paper_agent.agent_platform.finalize import (
            format_acceptance_note,
            run_acceptance,
        )

        try:
            outcome = run_acceptance(
                self._agent,
                self._session,
                self._messages,
                instruction=user_text,
                output_dir=self._output_dir,
                max_heal_rounds=self._acceptance_max_heal_rounds,
            )
        except Exception:  # noqa: BLE001 - 验收异常不影响主对话
            return ""
        return format_acceptance_note(outcome)

    def _persist(self) -> None:
        """每轮后持久化会话与 ask_user 问答（支持中断续跑）。"""
        if self._ask_tool is not None and getattr(self._ask_tool, "collected", None):
            self._repo.update(self._session.workspace, self._ask_tool.persist_mutation())
        save_session(self._repo, self._session)


# 撞到「工具调用轮数上限」时自动续跑的最大次数（防失控；token/时间预算才是硬闸）。
_AUTO_CONTINUE_LIMIT = 6
# 自动续跑时发给 agent 的内部提示。
_AUTO_CONTINUE_PROMPT = "继续完成上一条任务尚未完成的部分。"


def run_chat_repl(
    controller: ChatController,
    *,
    initial_message: str | None = None,
    input_fn: Callable[[str], str] = input,
    output_fn: Callable[[str], None] = print,
    streaming: bool = False,
    auto_continue_limit: int = _AUTO_CONTINUE_LIMIT,
) -> None:
    """极简终端 REPL。``initial_message`` 非空时作为首轮自动发送。

    ``streaming=True`` 时，助手答复由流式 sink 边生成边逐字打印，REPL 不再整段
    重复打印（仅打印前缀与收尾）；``False`` 时回退为一次性整段打印。

    **自动续跑**：一轮因「工具调用轮数上限」中断且仍在推进时，自动接着跑（至多
    ``auto_continue_limit`` 次），无需用户手动敲「继续」；若停滞（连续无新落盘改动）
    或触达 token/时间预算，则停止并交回用户。

    命令：``/exit`` 退出；``/files`` 列出本次产出文件；``/help`` 帮助。
    I/O 经参数注入，便于测试。
    """
    output_fn("论文写作助手（多轮对话）。输入 /exit 退出，/help 查看命令。")

    if initial_message and initial_message.strip():
        _handle_turn(controller, initial_message, output_fn, streaming, auto_continue_limit)

    while True:
        try:
            line = input_fn("\n你 > ").strip()
        except (EOFError, KeyboardInterrupt):
            output_fn("\n再见。")
            return
        if not line:
            continue
        if line in ("/exit", "/quit"):
            output_fn("再见。")
            return
        if line == "/help":
            output_fn("命令：/exit 退出；/files 列出产出文件；/help 帮助。直接输入即为对话。")
            continue
        if line == "/files":
            files = _collect_files(controller.session.transcript)
            output_fn("产出文件：" + ("、".join(files) if files else "（暂无）"))
            continue
        _handle_turn(controller, line, output_fn, streaming, auto_continue_limit)


def _handle_turn(
    controller: ChatController,
    text: str,
    output_fn,
    streaming: bool = False,
    auto_continue_limit: int = _AUTO_CONTINUE_LIMIT,
) -> None:
    turn = _one_turn(controller, text, output_fn, streaming)

    # 自动续跑：仅当因轮数上限中断、且仍在推进（有落盘改动）时继续。
    auto = 0
    while (
        turn.bound_hit == "max_iters"
        and turn.made_progress
        and auto < auto_continue_limit
    ):
        auto += 1
        output_fn(f"\n（未完，自动继续 {auto}/{auto_continue_limit}…）")
        turn = _one_turn(controller, _AUTO_CONTINUE_PROMPT, output_fn, streaming)

    if turn.bound_hit == "max_iters" and not turn.made_progress:
        output_fn("  ⚠ 似乎卡住了（无新进展）。可换种说法或拆小任务再试。")
    elif turn.bound_hit == "max_iters":
        output_fn(f"  ⚠ 达到自动续跑上限（{auto_continue_limit} 次）。如需继续请再说一句。")
    elif turn.bound_hit:
        output_fn(f"  ⚠ 触达上限：{turn.bound_hit}（token 预算或时间）。")


def _one_turn(controller: ChatController, text: str, output_fn, streaming: bool):
    """执行一轮并打印助手答复（流式 or 整段），返回 ChatTurn。"""
    if streaming:
        output_fn("\n助手 > ")
        turn = controller.send(text)
        # 工作流/取消等**非流式**路径的回复不会被实时打印，须在此显式输出，否则
        # 结果会被吞掉（用户会以为"选了就终止了"）。converse 路径已流式打印，不重复。
        if turn.reply and not turn.streamed:
            output_fn(turn.reply)
        output_fn("")  # 收尾换行
        if not turn.reply and not turn.tool_calls:
            output_fn("（无文本答复）")
    else:
        turn = controller.send(text)
        if turn.tool_calls:
            output_fn("  · 调用了工具：" + "、".join(turn.tool_calls))
        output_fn("\n助手 > " + (turn.reply or "（无文本答复）"))
    # 收尾验收提示（若本轮触发了验收）——流式与整段模式都在答复后单独打印。
    if turn.acceptance_note:
        output_fn(turn.acceptance_note)
    return turn


def _collect_files(transcript: list[dict]) -> list[str]:
    files: list[str] = []
    for e in transcript:
        for f in e.get("files", []) or []:
            if f not in files:
                files.append(f)
    return files


__all__ = ["ChatController", "ChatTurn", "run_chat_repl"]
