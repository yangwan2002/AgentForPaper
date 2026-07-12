"""平台装配：把工具、护栏、顶层循环与意图层接线为一个可运行的 ``PaperAgentApp``。

对外只暴露两个入口：``run_task(task)`` 与 ``resume(session_id)``。内部按会话动态构造
工具注册表（工具需绑定当前会话的 ``ToolContext``），并把完整管线作为 ``run_full_pipeline``
的运行器注入，实现「大任务复用旧管线、小任务用新工具」的混合调度。

依赖注入优先：核心依赖（llm/repo/gate/retrieval/verifier/pipeline_runner）经构造传入，
便于测试；``build_agent_app`` 提供据 ``Config`` 的一站式真实装配。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable

from paper_agent.agent_platform.guardrail_gate import GuardrailGate
from paper_agent.agent_platform.intake import TaskIntake
from paper_agent.agent_platform.models import (
    AgentSession,
    TaskAgentConfig,
    TaskResult,
    WritingTask,
)
from paper_agent.agent_platform.session_store import save_session
from paper_agent.agent_platform.task_agent import TaskAgent
from paper_agent.agent_platform.tools.ask import register_ask_user
from paper_agent.agent_platform.tools.context import ToolContext
from paper_agent.agent_platform.tools.edit import (
    register_add_section,
    register_edit_section_anchor,
    register_polish_section,
    register_rewrite_section,
)
from paper_agent.agent_platform.tools.export_tool import register_export_paper
from paper_agent.agent_platform.tools.full_pipeline import register_run_full_pipeline
from paper_agent.agent_platform.tools.import_draft import register_import_draft
from paper_agent.agent_platform.tools.locate import register_locate_section
from paper_agent.agent_platform.tools.read import register_read_section
from paper_agent.agent_platform.tools.references import (
    register_add_references,
    register_verify_existing_references,
)
from paper_agent.agent_platform.tools.typesetting_tool import register_set_typesetting
from paper_agent.elicitation import AutoElicitor, Elicitor
from paper_agent.observability.events import EventSink
from paper_agent.observability.usage import UsageTracker
from paper_agent.providers.llm.base import LLMProvider
from paper_agent.providers.retrieval.base import RetrievalProvider
from paper_agent.tools.citation import CitationVerifier
from paper_agent.tools.literature_tool import LiteratureSearchTool
from paper_agent.tools.registry import ToolRegistry
from paper_agent.workspace.models import OutputFormat
from paper_agent.workspace.repository import WorkspaceRepository

# TaskAgentConfig 各字段的合法区间（装配层校验，Req 越界拒绝）。
_CONFIG_RANGES = {
    "max_iters": (1, 100),
    "context_token_budget": (1, 1_000_000),
    "max_tool_result_tokens": (100, 100_000),
    "keep_recent_turns": (1, 50),
}


def validate_agent_config(cfg: TaskAgentConfig) -> None:
    """校验 TaskAgentConfig 取值范围；越界抛 ValueError（装配前拦截）。"""
    for field_name, (lo, hi) in _CONFIG_RANGES.items():
        value = getattr(cfg, field_name)
        if not (lo <= value <= hi):
            raise ValueError(
                f"TaskAgentConfig.{field_name}={value} 越界，合法区间 [{lo}, {hi}]。"
            )


@dataclass
class PaperAgentApp:
    """自然语言驱动的论文写作智能体平台的顶层运行器。"""

    llm: LLMProvider
    repo: WorkspaceRepository
    gate: GuardrailGate
    retrieval: RetrievalProvider
    verifier: CitationVerifier
    pipeline_runner: Callable[[str], object]
    elicitor: Elicitor = field(default_factory=AutoElicitor)
    tracker: UsageTracker | None = None
    sink: EventSink | None = None
    output_dir: str = "output"
    agent_config: TaskAgentConfig = field(default_factory=TaskAgentConfig)
    deadline_s: float = 0.0
    token_budget: int = 0
    default_output_format: OutputFormat = OutputFormat.MARKDOWN
    # 收尾验收：解析出可测约束时，任务收尾前跑确定性验收 + 有界自愈（Task 4）。
    # 关闭时行为与既有一致（Property 9）。
    enable_acceptance: bool = True
    acceptance_max_heal_rounds: int = 2
    # 按需只读评审（Task 5）：为 None 时不注册 review_paper 工具（行为不变）。
    reviewer_llm: LLMProvider | None = None
    adversarial_llm: LLMProvider | None = None
    # 意图路由 + 确定性工作流（intent-routing-and-workflows）：开启后多轮对话每轮先做
    # 意图路由，命中固定任务走确定性工作流。默认关闭 → 全部走既有 converse（向后兼容）。
    routing_enabled: bool = False
    routing_confidence_threshold: float = 0.75
    # 保格式润色的只读审计器（inplace-polish-audit）：非空时注入 InplacePolishWorkflow，
    # 润色同时附文献真伪 + 引用忠实性审计报告。None → 不审计（行为不变）。
    draft_auditor: object | None = None
    # 受沙箱代码执行工具（sandboxed-run-python）：低风险长尾工具层。sandbox_runner 非空
    # 且 run_python_enabled 时注册 run_python 工具。None → 不注册（行为不变）。
    run_python_enabled: bool = False
    sandbox_runner: object | None = None
    sandbox_timeout_s: float = 30.0
    sandbox_memory_mb: int = 512
    # 视觉版面验收闸（visual-layout-acceptance）：vlm 非空且 visual_enabled 时注入 gate。
    # None / 关闭 → ChatController 不做任何渲染/视觉调用（行为不变）。
    vlm: LLMProvider | None = None
    visual_enabled: bool = False
    visual_max_rounds: int = 1
    visual_dpi: int = 150
    visual_max_pages: int = 6
    soffice_path: str | None = None

    def __post_init__(self) -> None:
        validate_agent_config(self.agent_config)
        self._intake = TaskIntake(
            self.repo, default_output_format=self.default_output_format
        )

    # --- 对外入口 -----------------------------------------------------------

    def run_task(self, task: WritingTask) -> TaskResult:
        """受理并执行一个自然语言写作任务。"""
        session = self._intake.start(task)
        return self._run_session(session)

    def resume(self, session_id: str) -> TaskResult:
        """从既有会话续跑（Req 9.5）。"""
        session = self._intake.resume(session_id)
        return self._run_session(session)

    def open_chat(self, task: WritingTask, *, on_tool_call=None):
        """开启一段多轮对话，返回 ``ChatController``（路径 A）。

        任务的 ``instruction`` 作为对话的首条消息由调用方（REPL）发送；工作区、
        工具注册表与 agent 在整段对话中复用，保证多轮上下文与状态连续。
        """
        from paper_agent.agent_platform.chat import ChatController

        session = self._intake.start(task, require_instruction=False)
        agent, ask_tool, ctx = self._build_agent(session, on_tool_call=on_tool_call)
        return self._make_chat_controller(agent, session, ask_tool, ctx)

    def resume_chat(self, session_id: str, *, on_tool_call=None):
        """续跑一段既有会话为多轮对话。"""
        session = self._intake.resume(session_id)
        agent, ask_tool, ctx = self._build_agent(session, on_tool_call=on_tool_call)
        return self._make_chat_controller(agent, session, ask_tool, ctx)

    def _make_chat_controller(self, agent, session, ask_tool, ctx):
        """构造 ChatController，按需接入意图路由 + 确定性工作流。"""
        from paper_agent.agent_platform.chat import ChatController

        router = workflows = None
        if self.routing_enabled:
            router, workflows = self._build_routing(ctx)
        visual_gate = None
        if self.visual_enabled and self.vlm is not None:
            from paper_agent.agent_platform.visual.gate import VisualAcceptanceGate

            visual_gate = VisualAcceptanceGate(self.vlm, soffice_path=self.soffice_path)
        return ChatController(
            agent, session, self.repo, ask_tool=ask_tool,
            output_dir=self.output_dir,
            enable_acceptance=self.enable_acceptance,
            acceptance_max_heal_rounds=self.acceptance_max_heal_rounds,
            router=router,
            workflows=workflows,
            tool_context=ctx,
            routing_enabled=self.routing_enabled,
            confirm_threshold=self.routing_confidence_threshold,
            visual_gate=visual_gate,
            visual_enabled=self.visual_enabled and self.vlm is not None,
            visual_max_rounds=self.visual_max_rounds,
            visual_dpi=self.visual_dpi,
            visual_max_pages=self.visual_max_pages,
        )

    # --- 内部 ---------------------------------------------------------------

    def _build_agent(self, session: AgentSession, *, on_tool_call=None):
        """据会话装配工具注册表与 TaskAgent，返回 (agent, ask_tool, ctx)。"""
        ctx = ToolContext(
            session=session,
            repo=self.repo,
            gate=self.gate,
            elicitor=self.elicitor,
            output_dir=self.output_dir,
        )
        registry, ask_tool = self._build_registry(ctx)
        from paper_agent.agent_platform.finalize import make_acceptance_finalizer

        finalizer = make_acceptance_finalizer(
            self.output_dir,
            max_heal_rounds=self.acceptance_max_heal_rounds,
            enabled=self.enable_acceptance,
        )
        agent = TaskAgent(
            self.llm,
            registry,
            config=self.agent_config,
            tracker=self.tracker,
            deadline_s=self.deadline_s,
            token_budget=self.token_budget,
            sink=self.sink,
            on_tool_call=on_tool_call,
            acceptance_finalizer=finalizer,
        )
        return agent, ask_tool, ctx

    def _build_routing(self, ctx: ToolContext):
        """构造 IntentRouter + 固定任务工作流注册表（intent-routing-and-workflows）。

        路由用 agent 主 llm 做单选分类；保结构润色工作流用 reviewer_llm（无则回退主
        llm）与 inplace 工具保持一致。未启用路由时不影响装配（ChatController 侧关闭）。
        """
        from paper_agent.agent_platform.routing import Intent, IntentRouter
        from paper_agent.agent_platform.workflows import (
            ConvertWorkflow,
            InplacePolishWorkflow,
        )

        router = IntentRouter(
            self.llm,
            confidence_threshold=self.routing_confidence_threshold,
        )
        inplace_llm = self.reviewer_llm if self.reviewer_llm is not None else self.llm
        workflows = {
            Intent.CONVERT_FORMAT: ConvertWorkflow(),
            Intent.INPLACE_POLISH: InplacePolishWorkflow(
                inplace_llm, auditor=self.draft_auditor
            ),
        }
        return router, workflows

    def _run_session(self, session: AgentSession) -> TaskResult:
        agent, ask_tool, _ctx = self._build_agent(session)
        result = agent.run(session)

        # 持久化会话（transcript/task）与 ask_user 收集到的问答。
        if ask_tool is not None and ask_tool.collected:
            self.repo.update(session.workspace, ask_tool.persist_mutation())
        save_session(self.repo, session)
        return result

    def _build_registry(self, ctx: ToolContext):
        """按会话构造工具注册表，返回 (registry, ask_tool)。"""
        registry = ToolRegistry()

        # 只读工具。
        register_read_section(registry, ctx)
        register_locate_section(registry, ctx)
        register_export_paper(registry, ctx)
        ask_tool = register_ask_user(registry, ctx)

        # 按需只读评审（Task 5）：仅在配置了 reviewer_llm 时注册。
        if self.reviewer_llm is not None:
            from paper_agent.agent_platform.tools.review import register_review_paper

            register_review_paper(
                registry, ctx, self.reviewer_llm, self.adversarial_llm
            )

        # 文件导入（把用户本地论文读进工作区）。
        register_import_draft(registry, ctx)

        # DOCX 保结构处理（P0）：用户原 .docx 保留全部原格式做润色/排版，
        # 而非重建丢格式。用 reviewer_llm，否则回退主 llm 供保结构语言润色。
        from paper_agent.agent_platform.tools.docx_inplace_tool import (
            register_polish_docx_inplace,
        )

        inplace_llm = self.reviewer_llm if self.reviewer_llm is not None else self.llm
        register_polish_docx_inplace(registry, ctx, inplace_llm)

        # LaTeX 保结构润色（与 docx 对称）：原 .tex 保留 preamble/宏/公式/引用做语言润色。
        from paper_agent.agent_platform.tools.latex_inplace_tool import (
            register_polish_latex_inplace,
        )

        register_polish_latex_inplace(registry, ctx, inplace_llm)

        # 跨格式直转（P0）：用户原 .tex/.docx/.md 用 pandoc 直转目标格式，保公式/结构。
        from paper_agent.agent_platform.tools.convert_tool import (
            register_convert_document,
        )

        register_convert_document(registry, ctx)

        # 原稿就地增补（方案 C）：在原 .docx/.tex 上插入新章节 + 参考文献，保结构、
        # 不重建、不丢公式——「给成品稿补引言/加文献并保格式」的正确路径。
        from paper_agent.agent_platform.tools.augment_tool import (
            register_augment_document,
        )

        register_augment_document(registry, ctx)

        # 受沙箱代码执行（sandboxed-run-python）：低风险长尾工具层，仅在启用且后端可用时注册。
        # 绝不触碰正确性核心——工具不接收 repo/gate，沙箱内代码改不了工作区。
        if self.run_python_enabled and self.sandbox_runner is not None:
            from paper_agent.agent_platform.tools.run_python_tool import (
                register_run_python,
            )

            register_run_python(
                registry, ctx, self.sandbox_runner,
                default_timeout_s=self.sandbox_timeout_s,
                default_memory_mb=self.sandbox_memory_mb,
            )

        # 改工作区工具（经护栏 + 单一写路径）。
        register_rewrite_section(registry, ctx)
        register_polish_section(registry, ctx)
        register_add_section(registry, ctx)
        register_edit_section_anchor(registry, ctx)
        search_tool = LiteratureSearchTool(self.retrieval, self.verifier)
        register_add_references(registry, ctx, search_tool)
        register_verify_existing_references(registry, ctx, self.verifier)
        register_set_typesetting(registry, ctx)

        # 图浮动排版工具：把某张图设为浮动/页顶/跨栏满宽/上下环绕（对标 figure*[t]）。
        from paper_agent.agent_platform.tools.float_figure_tool import register_float_figure

        register_float_figure(registry, ctx)

        # 视觉版面校验的主动请求工具（visual-layout-acceptance）：仅在启用且配了多模态时
        # 暴露给模型；关闭时不注册（行为不变）。确定性触发不依赖它。
        if self.visual_enabled and self.vlm is not None:
            from paper_agent.agent_platform.tools.check_layout_tool import (
                register_check_layout,
            )

            register_check_layout(registry, ctx)

        # 复合工具：完整管线。
        register_run_full_pipeline(registry, ctx, self.pipeline_runner)

        return registry, ask_tool


def build_agent_app(
    config,
    *,
    store=None,
    sink: EventSink | None = None,
    tracker: UsageTracker | None = None,
    elicitor: Elicitor | None = None,
    agent_config: TaskAgentConfig | None = None,
) -> PaperAgentApp:
    """据 ``Config`` 一站式装配 ``PaperAgentApp``（复用既有 provider 与 Orchestrator）。

    - LLM / retrieval / verifier 经既有 factory 构造；
    - GuardrailGate 强制启用质量闸 + 引用真实性核验（忠实性深审留给 run_full_pipeline）；
    - run_full_pipeline 运行器 = 既有 Orchestrator 在同一 store 上 resume 运行。
    """
    from paper_agent.app import _wrap_llm_stack, build_orchestrator
    from paper_agent.providers.factory import (
        build_llm_provider,
        build_retrieval_provider,
        build_reviewer_llm_provider,
    )
    from paper_agent.tools.quality_gate import QualityGate
    from paper_agent.workspace.models import RetryPolicy
    from paper_agent.workspace.store import JsonFileStore

    config.validate()
    store = store or JsonFileStore(config.workspace_dir)
    repo = WorkspaceRepository(store)

    base_llm = build_llm_provider(config)
    retrieval = build_retrieval_provider(config)
    verifier = CitationVerifier(retrieval)
    reviewer_llm = build_reviewer_llm_provider(config)

    # 给 TaskAgent 的 LLM 套上 Observable(Resilient(...)) 栈：使 agent 循环也享有
    # 自动重试、用量统计与 token 预算闸（否则裸 provider 下 tracker 恒 0、预算失效）。
    policy = RetryPolicy(
        max_retries=config.retry_max_retries,
        base_backoff=config.retry_base_backoff,
        max_backoff=config.retry_max_backoff,
        jitter=config.retry_jitter,
    )
    # 追踪落盘（agent-observability-tracing）：开启时把用户 sink 与按 trace_id 分文件的
    # JsonLinesSink 组成 MultiSink，再包 TracingSink（自动补全 trace/span/ts）。未开启
    # 时下方逻辑与现状完全一致（向后兼容）。
    if getattr(config, "tracing_enabled", False):
        import os as _os

        from paper_agent.observability.sinks import (
            JsonLinesSink,
            MultiSink,
            TracingSink,
        )

        trace_dir = config.trace_dir or _os.path.join(config.workspace_dir, "traces")
        jsonl_sink = JsonLinesSink(
            directory=trace_dir, content_level=config.trace_content_level
        )
        downstream = [s for s in (sink, jsonl_sink) if s is not None]
        sink = TracingSink(MultiSink(downstream))

    # 用量统计不应依赖是否有控制台 sink：有 tracker 但无 sink 时，用 NullSink 触发
    # Observable 包装，使 token 统计与预算闸照常生效（无任何控制台输出）。
    effective_sink = sink
    if effective_sink is None and tracker is not None:
        from paper_agent.observability.events import NullSink

        effective_sink = NullSink()
    agent_llm = _wrap_llm_stack(
        base_llm,
        policy,
        effective_sink,
        tracker,
        config.event_preview_chars,
        config.total_token_budget,
        "writer",
    )

    # 增量写路径的忠实性筛查（#2）：只拦「引用明确不支撑声明」的造假，拿不到文献
    # 支撑材料一律放行。判定器优先用独立 reviewer 模型（打破自评偏置），否则回退 writer。
    from paper_agent.agent_platform.faithfulness_screener import (
        GuardrailFaithfulnessScreener,
    )
    from paper_agent.agents.citation_faithfulness_agent import (
        CitationFaithfulnessAgent,
        FaithfulnessJudge,
    )
    from paper_agent.parsing import StructuredParser

    judge_llm = reviewer_llm if reviewer_llm is not None else base_llm
    faithfulness_screener = GuardrailFaithfulnessScreener(
        FaithfulnessJudge(StructuredParser(judge_llm)),
        min_grounding_chars=config.min_grounding_chars,
        token_budget=config.faithfulness_token_budget,
        max_claims=getattr(config, "faithfulness_max_claims", 12),
        screen_deadline_s=getattr(config, "faithfulness_screen_deadline_s", 30.0),
    )
    gate = GuardrailGate(
        quality_gate=QualityGate(),
        citation_verifier=verifier,
        faithfulness_screener=faithfulness_screener,
    )

    # 完整管线运行器：与平台共享同一 store，经 resume 在当前工作区上运行。
    # 传入裸 base_llm——build_orchestrator 内部会自行套 Observable(Resilient(...))，
    # 避免与 agent_llm 双重包裹；二者共享同一 tracker，用量各自路径分别累计。
    orchestrator = build_orchestrator(
        llm=base_llm,
        retrieval=retrieval,
        config=config,
        store=store,
        sink=effective_sink,
        tracker=tracker,
        reviewer_llm=reviewer_llm,
        elicitor=elicitor,
    )

    def _pipeline_runner(workspace_id: str):
        return orchestrator.run(resume_id=workspace_id)

    # 保格式润色的只读审计器（inplace-polish-audit）：默认开启；mock LLM / mock 检索
    # 下自动降级（判定器置 None / retrieval_available=False），产出「不可核验」报告。
    draft_auditor = None
    if bool(getattr(config, "inplace_audit_enabled", True)):
        from paper_agent.agent_platform.audit import DraftAuditor

        retrieval_available = config.retrieval_provider not in ("mock", "", None)
        faith_agent = None
        if config.llm_provider != "mock":
            faith_agent = CitationFaithfulnessAgent(
                FaithfulnessJudge(StructuredParser(judge_llm)),
                min_grounding_chars=config.min_grounding_chars,
                token_budget=config.faithfulness_token_budget,
            )
        draft_auditor = DraftAuditor(
            verifier, faith_agent, retrieval_available=retrieval_available
        )

    # 受沙箱代码执行工具（sandboxed-run-python）：默认关闭;启用时按配置选后端,
    # 指定 docker 不可用则拒绝(runner=None → 不注册,不静默降级)。
    sandbox_runner = None
    if bool(getattr(config, "run_python_enabled", False)):
        from paper_agent.agent_platform.sandbox import select_sandbox

        sandbox_runner, _note = select_sandbox(
            getattr(config, "sandbox_backend", "auto"),
            image=getattr(config, "sandbox_image", "python:3.12-slim"),
        )

    # 视觉版面验收闸（visual-layout-acceptance）：默认关；开启时构造独立多模态 provider，
    # 未配置多模态（vlm 为 None）时 gate 侧优雅降级、不触发。
    vlm = None
    if bool(getattr(config, "visual_acceptance_enabled", False)):
        from paper_agent.providers.factory import build_vlm_provider

        try:
            vlm = build_vlm_provider(config)
        except Exception:  # noqa: BLE001 - 多模态装配失败 → 降级为不启用（不拖垮装配）
            vlm = None

    return PaperAgentApp(
        llm=agent_llm,
        repo=repo,
        gate=gate,
        retrieval=retrieval,
        verifier=verifier,
        pipeline_runner=_pipeline_runner,
        elicitor=elicitor or AutoElicitor(),
        tracker=tracker,
        sink=sink,
        output_dir=config.workspace_dir,
        agent_config=agent_config or TaskAgentConfig(),
        deadline_s=float(getattr(config, "wall_clock_deadline_s", 0.0) or 0.0),
        token_budget=int(getattr(config, "total_token_budget", 0) or 0),
        default_output_format=config.default_output_format,
        # 按需只读评审：主审用 base_llm，对抗审用独立 reviewer_llm（破自评偏置）。
        reviewer_llm=base_llm,
        adversarial_llm=reviewer_llm,
        # 意图路由 + 确定性工作流：据 Config 开关接入（默认开启）。
        routing_enabled=bool(getattr(config, "routing_enabled", True)),
        routing_confidence_threshold=float(
            getattr(config, "routing_confidence_threshold", 0.75)
        ),
        # 保格式润色只读审计器（inplace-polish-audit）。
        draft_auditor=draft_auditor,
        # 受沙箱代码执行工具（sandboxed-run-python）。
        run_python_enabled=bool(getattr(config, "run_python_enabled", False)),
        sandbox_runner=sandbox_runner,
        sandbox_timeout_s=float(getattr(config, "sandbox_timeout_s", 30.0)),
        sandbox_memory_mb=int(getattr(config, "sandbox_memory_mb", 512)),
        # 视觉版面验收闸（visual-layout-acceptance）。
        vlm=vlm,
        visual_enabled=bool(getattr(config, "visual_acceptance_enabled", False)) and vlm is not None,
        visual_max_rounds=int(getattr(config, "visual_acceptance_max_rounds", 1)),
        visual_dpi=int(getattr(config, "visual_render_dpi", 150)),
        visual_max_pages=int(getattr(config, "visual_max_pages", 6)),
        soffice_path=getattr(config, "soffice_path", None),
    )


__all__ = ["PaperAgentApp", "build_agent_app", "validate_agent_config"]
