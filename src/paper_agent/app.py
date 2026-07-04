"""组装：用给定 provider 装配一个可运行的 Orchestrator。

集中处理依赖注入，使调用方（CLI / 测试）无需了解内部接线。
"""

from __future__ import annotations

from paper_agent.agents.adversarial_review_agent import AdversarialReviewAgent
from paper_agent.agents.citation_audit_agent import CitationAuditAgent
from paper_agent.agents.citation_faithfulness_agent import (
    CitationFaithfulnessAgent,
    FaithfulnessJudge,
)
from paper_agent.agents.plan_agent import PlanAgent
from paper_agent.agents.review_agent import ReviewAgent
from paper_agent.agents.language_polish_agent import LanguagePolishAgent
from paper_agent.agents.search_agent import SearchAgent
from paper_agent.agents.terminology_agent import TerminologyAgent
from paper_agent.agents.writing_agent import WritingAgent
from paper_agent.config import Config
from paper_agent.context.manager import ContextManager
from paper_agent.context.tokenizer import build_token_counter
from paper_agent.export.format_gate import FormatGate
from paper_agent.export.format_repair import FormatRepairLoop
from paper_agent.hooks import Hooks
from paper_agent.observability.events import Event, EventKind, EventSink
from paper_agent.observability.llm_wrapper import ObservableLLMProvider
from paper_agent.observability.usage import UsageTracker
from paper_agent.orchestrator import Orchestrator
from paper_agent.parsing import StructuredParser
from paper_agent.providers.factory import (
    build_llm_provider,
    build_retrieval_provider,
    build_reviewer_llm_provider,
)
from paper_agent.providers.llm.base import LLMProvider
from paper_agent.providers.llm.mock import MockLLMProvider
from paper_agent.providers.llm.resilient import ResilientLLMProvider
from paper_agent.providers.retrieval.base import RetrievalProvider
from paper_agent.tools.citation import CitationVerifier
from paper_agent.tools.citation_parser import CitationParser
from paper_agent.workspace.models import RetryPolicy
from paper_agent.workspace.repository import WorkspaceRepository
from paper_agent.workspace.store import JsonFileStore, WorkspaceStore


def _wrap_llm_stack(
    base: LLMProvider,
    policy: RetryPolicy,
    sink: EventSink | None,
    tracker: UsageTracker | None,
    preview_chars: int = 500,
) -> LLMProvider:
    """统一装配 Observable(Resilient(base)) 调用栈。"""
    stack: LLMProvider = ResilientLLMProvider(base, policy, sink)
    if sink is not None:
        stack = ObservableLLMProvider(
            stack, sink, tracker, preview_chars=preview_chars
        )
    return stack


def build_orchestrator(
    llm: LLMProvider,
    retrieval: RetrievalProvider,
    config: Config,
    store: WorkspaceStore | None = None,
    sink: EventSink | None = None,
    tracker: UsageTracker | None = None,
    hooks: Hooks | None = None,
    reviewer_llm: LLMProvider | None = None,
    elicitor=None,
) -> Orchestrator:
    """装配 Orchestrator。

    Round 4：``reviewer_llm`` 显式传入则作为 reviewer 的底层 provider；
    缺省时回退 writer 的 ``llm``（向后兼容，但二者共享 LLM 实例是 reward-hack
    温床——生产部署建议显式注入不同模型的 reviewer_llm）。
    """
    store = store or JsonFileStore(config.workspace_dir)
    repo = WorkspaceRepository(store)
    verifier = CitationVerifier(retrieval)
    hooks = hooks or Hooks()

    # 在装配 LLM 调用栈前，先据具体 provider 探测是否为 Mock/测试 provider。
    # 该标记驱动结构化解析的优雅降级：Mock 失败回退、生产失败显式暴露（升级 Req 1/3）。
    writer_base = llm
    is_mock = isinstance(writer_base, MockLLMProvider)

    # 装配 writer LLM 调用栈：Observable(Resilient(具体 provider))。
    # 健壮性层始终叠在具体 provider 外；可观测层仅在提供 sink 时叠在最外层。
    # 重试策略与预览长度均从 Config 读取（此前硬编码）。
    policy = RetryPolicy(
        max_retries=config.retry_max_retries,
        base_backoff=config.retry_base_backoff,
        max_backoff=config.retry_max_backoff,
        jitter=config.retry_jitter,
    )
    preview_chars = config.event_preview_chars
    writer_llm = _wrap_llm_stack(writer_base, policy, sink, tracker, preview_chars)

    # Round 4：reviewer 用独立的 LLM 实例（打破自评 reward-hack）。
    # 未显式传入时回退 writer——记一条警示日志便于发现配置遗漏。
    reviewer_base = reviewer_llm if reviewer_llm is not None else writer_base
    reviewer_is_mock = isinstance(reviewer_base, MockLLMProvider)
    # 自评 fail-closed（生产安全）：reviewer 回退复用 writer 的真实 LLM 会造成
    # 「模型自己评自己写的东西」的 reward-hack。真实 provider 下默认拒绝装配，
    # 除非显式 allow_self_review=True。Mock/测试 provider 不受限（保证既有测试与
    # 零配置骨架仍可跑）。
    if reviewer_base is writer_base and not is_mock and not config.allow_self_review:
        raise ValueError(
            "拒绝装配：reviewer 未配置独立 LLM，将复用 writer 的真实模型，构成自评 "
            "reward-hack 风险。请通过 reviewer_llm_provider/reviewer_llm_model 等配置"
            "为 reviewer 指定独立模型/端点；若确需同模型自评，请显式设置 "
            "Config.allow_self_review=True。"
        )
    if reviewer_base is writer_base and not is_mock and config.allow_self_review and sink is not None:
        sink.emit(
            Event(
                kind=EventKind.AGENT_LOG,
                message=(
                    "[警告] 已显式允许 writer 与 reviewer 共享同一 LLM 实例"
                    "（allow_self_review=True），存在自评 reward-hack 风险。"
                ),
            )
        )
    if reviewer_base is not writer_base:
        reviewer_llm_stack = _wrap_llm_stack(
            reviewer_base, policy, sink, tracker, preview_chars
        )
    else:
        reviewer_llm_stack = writer_llm

    # 共享结构化解析器：包装装饰后的 LLM 栈，供需要 JSON 解析的智能体统一复用（Req 3.9）。
    # is_mock 由解析器实例一次性持有（#12），各智能体调用 request_json 时无需再传。
    parser = StructuredParser(writer_llm, is_mock=is_mock)
    # Reviewer 走独立 parser（绑定到 reviewer LLM 栈）；与 writer 共享栈且 mock 标记
    # 一致时复用同一 parser，避免重复构造。
    if reviewer_llm_stack is writer_llm and is_mock == reviewer_is_mock:
        reviewer_parser = parser
    else:
        reviewer_parser = StructuredParser(
            reviewer_llm_stack, is_mock=reviewer_is_mock
        )

    # 全局统一 token 计量器：据目标模型构造单一 counter，注入上下文管理、写作智能体
    # 与用量统计，保证裁剪/截断/统计全程口径一致（Req 7.5/7.6）。
    counter = build_token_counter(config.llm_model or "")
    if tracker is not None:
        tracker.counter = counter

    context = ContextManager(writer_llm, counter=counter)
    audit_agent = CitationAuditAgent(CitationParser(parser=parser), verifier)

    # Round 4：对抗式评审（默认启用，可经 Config 关闭）。
    adversarial_agent = None
    if config.adversarial_review_enabled:
        adversarial_agent = AdversarialReviewAgent(
            reviewer_llm_stack,
            parser=reviewer_parser,
            is_mock=reviewer_is_mock,
            counter=counter,
            review_token_budget=config.review_token_budget,
            min_weaknesses=config.adversarial_min_weaknesses,
        )

    # citation-faithfulness-audit（任务 10.1）：声明级引用忠实性审计（默认关闭）。
    # 仅当 config.citation_faithfulness_enabled 为真时装配——复用 reviewer LLM 栈
    # 与 reviewer_parser（忠实性判定属评审类任务，复用 Observable 包裹的栈使用量
    # 自动纳入统计）。关闭时传 None → 不接入反馈闭环，行为逐字节不变（Req 8.1/8.2）。
    faithfulness_agent = None
    if config.citation_faithfulness_enabled:
        faithfulness_agent = CitationFaithfulnessAgent(
            FaithfulnessJudge(reviewer_parser),
            min_grounding_chars=config.min_grounding_chars,
            token_budget=config.faithfulness_token_budget,
            is_mock=reviewer_is_mock,
            sink=sink,
        )

    # 语言润色智能体（默认启用，可经 Config 关闭）：复用 writer LLM 栈。
    # Mock provider 装配时该 agent 自身 no-op（is_mock=True），输出逐字节不变，
    # 保证既有基于 Mock 的测试不回归。
    language_polish_agent = None
    if config.language_polish_enabled:
        language_polish_agent = LanguagePolishAgent(
            writer_llm, is_mock=is_mock, enabled=True
        )

    # 动态澄清问题提出器（路径 B，默认关闭）：复用 writer parser 请求 JSON。
    # 仅在交互式 Elicitor 下真正触发（见 Orchestrator._llm_clarify）。
    clarification_proposer = None
    if config.llm_clarifying_questions_enabled:
        from paper_agent.clarification import ClarificationProposer

        clarification_proposer = ClarificationProposer(
            parser, max_questions=config.max_clarifying_questions
        )

    # 术语抽取智能体（默认启用）：语言润色前填充 ws.glossary 供统一用词。
    # 复用 writer LLM 栈 + writer parser；Mock provider 下自身 no-op（is_mock）。
    terminology_agent = None
    if config.terminology_extraction_enabled:
        terminology_agent = TerminologyAgent(
            writer_llm, parser=parser, is_mock=is_mock, max_terms=config.max_terms
        )

    # 被引文献正文抓取器（默认关闭；涉及网络、best-effort）：开启时富化 ref.full_text
    # 供忠实性审计做正文级 grounding。Mock 场景一般不开启（无真实 pdf_url）。
    full_text_fetcher = None
    if getattr(config, "grounding_fulltext_enabled", False):
        from paper_agent.tools.reference_enrichment import build_fetcher

        full_text_fetcher = build_fetcher(
            timeout_s=config.grounding_fulltext_timeout_s
        )

    # format-pipeline-and-diff-revision（任务 21.1）：装配确定性格式闸 + 有界修复
    # 循环并注入 Orchestrator。二者对缺失工具（pandoc/pdflatex）优雅降级——在无
    # 相应工具的环境（如测试/CI）中不会中止管线，仅在导出说明中标注。修复循环复用
    # writer 的 LLM 栈产出候选修复（工具退出码仍是唯一裁判）。
    format_gate = FormatGate(
        format_gate_timeout=config.format_gate_timeout,
        enable_pdflatex_check=config.enable_pdflatex_check,
    )
    format_repair_loop = FormatRepairLoop(
        writer_llm,
        format_gate,
        max_repair_attempts=config.max_repair_attempts,
    )

    return Orchestrator(
        repo=repo,
        plan_agent=PlanAgent(writer_llm, parser=parser, is_mock=is_mock),        search_agent=SearchAgent(
            retrieval, verifier, writer_llm, parser=parser, is_mock=is_mock
        ),
        writing_agent=WritingAgent(
            writer_llm, context, retrieval=retrieval, verifier=verifier,
            counter=counter, hooks=hooks,
            # venue-templates-figures-tables：把数据出图能力接进生产装配。
            # 缺这几个参数时 WritingAgent 会静默跳过数据出图（此前的装配缺口）。
            figures_from_data_enabled=config.figures_from_data_enabled,
            workspace_dir=config.workspace_dir,
            sink=sink,
            # 写作期 ask_user：注入 Elicitor（仅交互式时写作智能体才暴露该工具）。
            elicitor=elicitor,
        ),
        review_agent=ReviewAgent(
            reviewer_llm_stack,
            parser=reviewer_parser,
            is_mock=reviewer_is_mock,
            counter=counter,
            review_token_budget=config.review_token_budget,
        ),
        config=config,
        sink=sink,
        audit_agent=audit_agent,
        hooks=hooks,
        usage_tracker=tracker,
        adversarial_review_agent=adversarial_agent,
        format_gate=format_gate,
        format_repair_loop=format_repair_loop,
        faithfulness_agent=faithfulness_agent,
        language_polish_agent=language_polish_agent,
        elicitor=elicitor,
        clarification_proposer=clarification_proposer,
        terminology_agent=terminology_agent,
        full_text_fetcher=full_text_fetcher,
    )


def build_from_config(
    config: Config,
    store: WorkspaceStore | None = None,
    sink: EventSink | None = None,
    tracker: UsageTracker | None = None,
    hooks: Hooks | None = None,
    elicitor=None,
) -> Orchestrator:
    """据配置选择 provider 并装配 Orchestrator（mock / openai / api）。

    Round 4：若配置了 ``reviewer_llm_*`` 字段，则为 reviewer 单独构造 LLM
    provider（不同模型/端点），与 writer 实例分离。

    ``elicitor``：用户澄清问答器（缺省 None → Orchestrator 用 AutoElicitor 非交互）。
    CLI 交互模式传入 ``CLIElicitor`` 以在草稿修订时就修订范围/补章节征询用户。
    """
    # 任务 21.1：装配层显式范围校验（Req 12.2）。越界的 format-pipeline 参数在
    # 组装前即以 ValueError 拒绝，避免把非法配置带入运行期。校验通过返回 None。
    config.validate()
    return build_orchestrator(
        llm=build_llm_provider(config),
        retrieval=build_retrieval_provider(config),
        config=config,
        store=store,
        sink=sink,
        tracker=tracker,
        hooks=hooks,
        reviewer_llm=build_reviewer_llm_provider(config),
        elicitor=elicitor,
    )
