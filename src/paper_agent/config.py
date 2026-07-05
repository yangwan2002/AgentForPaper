"""系统配置。

集中管理可调参数与 provider 选择，通过依赖注入传给 Orchestrator，
使主流程不依赖任何具体实现（依赖倒置）。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from paper_agent.workspace.models import OutputFormat, ScoringDimension


@dataclass
class Config:
    """运行时配置。

    Attributes:
        quality_threshold: 各评分维度判定达标的得分下限（Req 8.1）。
        iteration_limit: 写作—评审反馈循环的最大轮数（Req 8.2）。
        default_output_format: 用户未指定时使用的默认输出格式（Req 10.2）。
        workspace_dir: 工作区持久化的根目录。
        llm_provider: LLM provider 选择，"mock" | 预设厂商名 | "custom"。
        llm_model: LLM 模型名；为空则用厂商预设的默认模型。
        llm_base_url: 覆盖/指定 OpenAI 兼容端点（接入未预设厂商时使用）。
        llm_api_key_env: 读取 API Key 的环境变量名（接入未预设厂商时使用）。
        retrieval_provider: 检索 provider 选择，"mock" | "api"。
    """

    quality_threshold: float = 8.0
    iteration_limit: int = 5
    default_output_format: OutputFormat = OutputFormat.MARKDOWN
    workspace_dir: str = ".paper_workspaces"

    # 评审单次调用的 token 预算上限（#8）：长论文按章节均分截断，避免撑爆上下文。
    review_token_budget: int = 60000
    # 全局输出 token 预算上限（#19）：UsageTracker 累计超出后编排器降级（跳过
    # 进一步修订轮，直接进入导出），防止失控耗费。0 表示不限。
    total_token_budget: int = 0
    # 全局墙钟超时（秒）：反馈循环每轮开始时检查，超出即降级终止并直接导出，
    # 防止异常场景无限跑烧 token/时间。0 表示不限（库默认不限以兼容测试；
    # 生产入口应设非 0，见 scripts/run_real.py）。
    wall_clock_deadline_s: float = 0.0

    # LLM 健壮性重试策略（此前硬编码在 app.py，现可经 Config 调）。
    retry_max_retries: int = 3
    retry_base_backoff: float = 1.0
    retry_max_backoff: float = 30.0
    retry_jitter: float = 0.25

    # 自评守卫：reviewer 回退复用 writer LLM 是自评 reward-hack 温床。生产（真实
    # provider）下默认 fail-closed——除非显式置 True 或 reviewer 用了独立 provider，
    # 否则装配报错。Mock/测试 provider 不受此限（见 app.build_orchestrator）。
    allow_self_review: bool = False

    # 事件流中请求预览的最大字符数（防用户论文正文经落盘 sink 泄漏）。
    # 由装配层注入 ObservableLLMProvider；0 表示不预览（完全脱敏）。
    event_preview_chars: int = 500

    # 追踪落盘（agent-observability-tracing）：开启后一次运行/对话的所有事件按 trace
    # 落成 JSONL，供事后回放/归因。默认关闭 → 装配与行为与现状一致（向后兼容）。
    # trace_dir 为空时默认 <workspace_dir>/traces；trace_content_level ∈ full|redacted|off，
    # 与 event_preview_chars（终端脱敏）相互独立（落盘可全量、终端仍脱敏）。
    tracing_enabled: bool = False
    trace_dir: str = ""
    trace_content_level: str = "full"

    # 增量忠实性深审的单次核验预算（防大章节 add_section 逐句串行核验卡住）：
    # 单次落盘最多核验 faithfulness_max_claims 句、总耗时不超过
    # faithfulness_screen_deadline_s 秒，超出放行剩余。<=0 表示不限（严格模式）。
    faithfulness_max_claims: int = 12
    faithfulness_screen_deadline_s: float = 30.0

    # 意图路由 + 确定性工作流（intent-routing-and-workflows）：开启后 ChatController
    # 每轮先做意图路由——命中「转格式 / 保结构润色」等固定任务则走确定性工作流（工具
    # 序列写死、不经 LLM 编排）、执行前回显确认；开放任务仍走既有自由智能体。默认开启；
    # 置 False 时全部走既有 converse（逐字节向后兼容）。
    routing_enabled: bool = True
    # 固定任务执行前回显确认的置信阈值：低于此值走澄清（让用户在候选意图里选）。
    routing_confidence_threshold: float = 0.75

    # 保格式润色的只读审计旁路（inplace-polish-audit）：开启后 InplacePolishWorkflow
    # 在产出润色稿的同时，对原稿只读审计——核验已有参考文献真伪 + 引用忠实性，附一份
    # 建议性问题清单。只读、隔离、故障隔离；mock/无检索 provider 下自动降级为「不可核验」
    # 报告，不崩溃、不阻断润色。默认开启；关闭时润色行为逐字节不变。
    inplace_audit_enabled: bool = True

    llm_provider: str = "mock"
    llm_model: str = ""
    llm_base_url: str | None = None
    llm_api_key_env: str | None = None
    retrieval_provider: str = "mock"

    # --- Venue 模板 / 图表生成配置（venue-templates-figures-tables） ---
    # venue_id: 会场/期刊模板标识。Venue_Id 选择优先级（在后续导出/写作接入处
    # 消费；本任务仅加字段）：ws.profile["venue_id"] > config.venue_id > "default"。
    venue_id: str = "default"
    # figures_from_data_enabled: 是否允许从数据自动生成图表（Req 6.7）。
    figures_from_data_enabled: bool = True
    # figure_float_decimals: 图表/表格数值格式化保留的小数位数（Req 7.6）。
    figure_float_decimals: int = 3
    # styles_dir: 用户提供的会议样式文件目录（放置从会议官网/Overleaf 下载的
    # .sty/.cls）；由导出器经 ws.profile["styles_dir"] 消费。可选路径，validate() 不校验。
    styles_dir: str | None = None

    # --- Reviewer LLM 配置（Round 4：打破自评 reward-hack） ---
    # writer 与 reviewer 共享同一 LLM 实例是 reward-hack 的天然温床（模型自己评
    # 自己写的东西）。下列字段允许给 reviewer 配独立的 provider/model/端点，使
    # 主审与对抗审都走「不同模型」评判主管线产出。任一字段为空则该项回退到 writer
    # 的对应字段——零配置时仍可工作（同模型，至少 sampling 略不同）。
    reviewer_llm_provider: str = ""
    reviewer_llm_model: str = ""
    reviewer_llm_base_url: str | None = None
    reviewer_llm_api_key_env: str | None = None

    # 对抗式评审开关 & 最少 weakness 条数。
    adversarial_review_enabled: bool = True
    adversarial_min_weaknesses: int = 3

    # --- format-pipeline-and-diff-revision ---
    # Part A：补丁优先增量修订。
    # patch_first_enabled: 是否默认启用「补丁优先」修订路由（Req 1.7）。
    patch_first_enabled: bool = True
    # patch_size_limit: 补丁累计影响占比阈值，取值 0.0–1.0（Req 3.2）；
    # 单章内成功补丁累计影响占比超过此值即回退为整章重写。
    patch_size_limit: float = 0.5

    # Part B：pandoc 导出管线与降级策略。
    # pandoc_degrade_strategy: pandoc 不可用时的降级策略，取值 {fallback, fail_fast}（Req 8.6）。
    pandoc_degrade_strategy: str = "fallback"
    # pandoc_probe_timeout: pandoc 可用性探测的超时（秒）（Req 8.1）。
    pandoc_probe_timeout: float = 5.0

    # 格式闸（Format_Gate）配置。
    # enable_pdflatex_check: 是否对 LaTeX 产物额外运行 pdflatex 编译校验（Req 9.2）。
    enable_pdflatex_check: bool = False
    # format_gate_timeout: 格式闸单次工具运行超时（秒），取值 1–600（Req 9.7）。
    format_gate_timeout: int = 60

    # 格式修复循环（Format_Repair_Loop）配置。
    # max_repair_attempts: 修复循环最大尝试次数，取值 0–10（Req 10.3 / 11.1）；
    # 使工具链运行总次数 ≤ max_repair_attempts + 1。
    max_repair_attempts: int = 3

    # --- citation-faithfulness-audit（引用忠实性审计） ---
    # citation_faithfulness_enabled: 是否启用引用忠实性审计阶段（Req 8.1）。
    # 默认关闭：未装配时管线行为逐字节不变，加法式接入、向后兼容。
    citation_faithfulness_enabled: bool = False
    # min_grounding_chars: grounding 文本经 strip 后的最小字符数（Req 8.3）；
    # 低于此阈值即安全落 cannot_verify、不触发判定器。非负。
    min_grounding_chars: int = 40
    # faithfulness_token_budget: 喂入判定器的 grounding 文本字符预算上限（Req 8.3）；
    # 防御式截断至该上限，须 >= 1。
    faithfulness_token_budget: int = 4000

    # --- 被引文献正文富化（Round 9：把 grounding 从 abstract 扩到正文） ---
    # grounding_fulltext_enabled: 检索后对有 pdf_url 的已验证文献抓取正文填充
    # full_text，供忠实性审计做正文级 grounding、消解假阴 cannot_verify。涉及网络、
    # best-effort，默认关闭。
    grounding_fulltext_enabled: bool = False
    # grounding_fulltext_max_refs: 单次运行富化的文献条数上限（限网络调用）。
    grounding_fulltext_max_refs: int = 20
    # grounding_fulltext_timeout_s: 单条 PDF 下载超时（秒）。
    grounding_fulltext_timeout_s: float = 15.0

    # --- 语言润色 / 原创性自检（投递质量增强） ---
    # language_polish_enabled: 反馈循环收敛后、导出前运行一次独立语言润色 pass
    # （逐章节做语言/一致性改写，严格保真事实/数据/引用/结构）。Mock provider 下
    # 自动 no-op。默认开启。
    language_polish_enabled: bool = True
    # originality_check_enabled: 导出前对每章做与已核验文献的 n-gram 重合度自检，
    # 高重合记为可投递性 caution（不阻断导出）。默认开启。
    originality_check_enabled: bool = True
    # originality_ngram: 重合度自检的 n-gram 长度（词级），须 >= 1。
    originality_ngram: int = 8
    # originality_overlap_threshold: 单边覆盖率阈值，取值 (0.0, 1.0]，超过即告警。
    originality_overlap_threshold: float = 0.15

    # --- 动态澄清问题（路径 B：LLM 据场景提出，受数量约束） ---
    # llm_clarifying_questions_enabled: 是否在澄清阶段让 LLM 提出至多 N 条澄清问题。
    # 仅在交互式 Elicitor 下真正触发（非交互零影响）。默认关闭。
    llm_clarifying_questions_enabled: bool = False
    # max_clarifying_questions: LLM 澄清问题数量上限，须 >= 0（0 等于关闭）。
    max_clarifying_questions: int = 3

    # --- 术语抽取（主动构建术语表，供语言润色统一用词） ---
    # terminology_extraction_enabled: 语言润色前运行一次术语抽取，把核心术语规范写法
    # 写入 ws.glossary（不覆盖用户已提供）。Mock provider 下自动 no-op。默认开启。
    terminology_extraction_enabled: bool = True
    # max_terms: 抽取术语数量上限，须 >= 1。
    max_terms: int = 15

    # 各维度可单独设阈值，缺省回退到 quality_threshold。
    dimension_thresholds: dict[ScoringDimension, float] = field(default_factory=dict)

    def threshold_for(self, dimension: ScoringDimension) -> float:
        """返回某评分维度的达标阈值。"""
        return self.dimension_thresholds.get(dimension, self.quality_threshold)

    def validate(self) -> None:
        """装配层范围校验：在构建 provider / 组装管线前显式调用。

        校验 format-pipeline-and-diff-revision 引入的可调参数取值范围
        （Req 3.2 / 8.6 / 9.7 / 10.3）。越界或非法值以 1–500 字符的
        ``ValueError`` 拒绝，错误信息指明字段名、当前值与允许取值/范围。
        全部合法时返回 ``None``。

        注意：本方法**不**在 ``__post_init__`` 中自动调用，以保持向后兼容
        （既有测试可能构造超范围的临时 Config）；它是装配层的显式检查点。
        """
        if not (0.0 <= self.patch_size_limit <= 1.0):
            raise ValueError(
                f"patch_size_limit={self.patch_size_limit!r} 越界，"
                f"允许范围为 [0.0, 1.0]。"
            )
        if not (1 <= self.format_gate_timeout <= 600):
            raise ValueError(
                f"format_gate_timeout={self.format_gate_timeout!r} 越界，"
                f"允许范围为 [1, 600]（秒）。"
            )
        if not (0 <= self.max_repair_attempts <= 10):
            raise ValueError(
                f"max_repair_attempts={self.max_repair_attempts!r} 越界，"
                f"允许范围为 [0, 10]。"
            )
        allowed_strategies = ("fallback", "fail_fast")
        if self.pandoc_degrade_strategy not in allowed_strategies:
            raise ValueError(
                f"pandoc_degrade_strategy={self.pandoc_degrade_strategy!r} 非法，"
                f"允许取值为 {{fallback, fail_fast}}。"
            )

        # citation-faithfulness-audit 阈值范围校验（Req 8.3 / 8.4）：
        # 与上面 format-pipeline 的「越界即拒绝」不同，忠实性审计的阈值越界
        # 采取「静默回退到文档化默认值」而非抛致命异常——审计属加法式、默认
        # 关闭的可选特性，非法阈值不应中断整个装配。代码库尚无 logger，故直接
        # 重置属性并留注释记录该回退（默认 min_grounding_chars=40 /
        # faithfulness_token_budget=4000）。
        # min_grounding_chars 需非负（TypeError 兜底非数值输入）。
        try:
            invalid_min = self.min_grounding_chars < 0
        except TypeError:
            invalid_min = True
        if invalid_min:
            # 回退：非法 min_grounding_chars 重置为文档化默认 40。
            self.min_grounding_chars = 40
        # faithfulness_token_budget 需 >= 1（TypeError 兜底非数值输入）。
        try:
            invalid_budget = self.faithfulness_token_budget < 1
        except TypeError:
            invalid_budget = True
        if invalid_budget:
            # 回退：非法 faithfulness_token_budget 重置为文档化默认 4000。
            self.faithfulness_token_budget = 4000

        # 原创性自检参数范围校验（越界静默回退到文档化默认，与忠实性同风格，
        # 不中断装配）。originality_ngram 需 >= 1；overlap_threshold 需在 (0, 1]。
        try:
            invalid_ngram = self.originality_ngram < 1
        except TypeError:
            invalid_ngram = True
        if invalid_ngram:
            self.originality_ngram = 8
        try:
            invalid_overlap = not (0.0 < self.originality_overlap_threshold <= 1.0)
        except TypeError:
            invalid_overlap = True
        if invalid_overlap:
            self.originality_overlap_threshold = 0.15

        # 动态澄清问题数量上限：非负；非法回退默认 3。
        try:
            invalid_maxq = self.max_clarifying_questions < 0
        except TypeError:
            invalid_maxq = True
        if invalid_maxq:
            self.max_clarifying_questions = 3

        return None
