"""编排器：工作流调度与反馈循环。

职责：
- 接收请求，识别输入模式（Req 1）并初始化工作区。
- 调度规划 → 检索 → 写作—评审反馈循环 → 导出。
- 反馈循环终止条件：全维度达标 或 达到迭代上限（Req 8）。

编排器依赖抽象的 Agent 与 provider，不依赖具体实现（依赖倒置）。
所有工作区写入都经仓储原子落盘。
"""

from __future__ import annotations

import time
import uuid
from copy import deepcopy
from dataclasses import dataclass, field

from paper_agent.agents.base import Agent, AgentContext, AgentResult
from paper_agent.config import Config
from paper_agent.elicitation import AutoElicitor, Elicitor
from paper_agent.export.base import ExportResult
from paper_agent.export.factory import get_exporter
from paper_agent.export.format_models import FormatGateReport, RepairTerminalStatus
from paper_agent.hooks import Hooks
from paper_agent.observability.events import Event, EventKind, EventSink, NullSink
from paper_agent.observability.budget import (
    BudgetExceededError,
    RunBudgetContext,
    activate_run_budget,
    current_run_budget,
    reset_run_budget,
)
from paper_agent.observability.usage import UsageTracker
from paper_agent.workspace.models import (
    InputMode,
    OutlineNode,
    OutputFormat,
    PaperWorkspace,
    ParseStatus,
    ScoringDimension,
)
from paper_agent.workspace.repository import WorkspaceRepository
from paper_agent.workspace.research_artifact import ResearchArtifact


@dataclass
class PaperRequest:
    """用户发起的论文写作请求。"""

    draft: str | None = None              # 已有初稿（→ 草稿修订模式）
    topic_background: str | None = None    # 主题背景（→ 从零生成模式）
    output_format: OutputFormat | None = None
    figures: list = field(default_factory=list)
    profile: dict = field(default_factory=dict)  # 论文档案 / steering 偏好
    # Round 7：用户提供的真实研究内容（结构化）。GENERATION 模式建议必备——
    # 缺则系统显式降级为「LLM 推断版」，并在 export 顶部标注。
    artifact: "ResearchArtifact | None" = None


@dataclass
class PaperResult:
    workspace_id: str
    # "quality_met" | "iteration_limit" | "iteration_limit_unparsed_review"
    # | "stagnation" | "budget_exceeded"
    terminated_reason: str
    unmet_dimensions: list[ScoringDimension]
    export: ExportResult | None
    # 可投递性判定（新增，向后兼容默认值）：submittable 为是否满足全部硬约束，
    # submittability_notes 为人类可读的原因/风险清单。既有调用方不读这两个字段
    # 时行为不变。
    submittable: bool = True
    submittability_notes: list[str] = field(default_factory=list)


class InputValidationError(Exception):
    """请求缺少必要输入（Req 1.3）。"""


class Orchestrator:
    def __init__(
        self,
        repo: WorkspaceRepository,
        plan_agent: Agent,
        search_agent: Agent,
        writing_agent: Agent,
        review_agent: Agent,
        config: Config,
        sink: EventSink | None = None,
        audit_agent: Agent | None = None,
        quality_gate=None,
        hooks: Hooks | None = None,
        usage_tracker: UsageTracker | None = None,
        adversarial_review_agent: Agent | None = None,
        format_gate=None,
        format_repair_loop=None,
        faithfulness_agent: Agent | None = None,
        language_polish_agent: Agent | None = None,
        elicitor: Elicitor | None = None,
        clarification_proposer=None,
        terminology_agent: Agent | None = None,
        full_text_fetcher=None,
    ) -> None:
        self._repo = repo
        self._plan = plan_agent
        self._search = search_agent
        self._writing = writing_agent
        self._review = review_agent
        self._config = config
        self._sink = sink or NullSink()
        self._audit = audit_agent
        if quality_gate is None:
            from paper_agent.tools.quality_gate import QualityGate

            quality_gate = QualityGate()
        self._gate = quality_gate
        # #15：可插拔扩展点（智能体/工具调用前后）。
        self._hooks = hooks or Hooks()
        # #19：全局 token 预算闸（超额降额）。
        self._tracker = usage_tracker
        # Round 4：对抗式评审（可选）。提供时与主审联合判定达标——
        # 主审高分 AND 对抗审 accept 才算 quality_met，打破自评 reward-hack。
        self._adversarial = adversarial_review_agent
        # format-pipeline-and-diff-revision（任务 21.1）：可选注入的确定性格式闸
        # 与有界格式修复循环（依赖倒置，Req 12.2）。二者缺省为 None——不注入时
        # 保持原导出行为（Markdown 从不经闸；LaTeX/docx 也不校验），使既有直接
        # 构造 Orchestrator 的测试行为不变。
        self._format_gate = format_gate
        self._format_repair_loop = format_repair_loop
        # citation-faithfulness-audit（任务 9.1）：可选注入的声明级引用忠实性审计
        # 智能体。缺省 None → 不装配、不接入反馈闭环，系统行为逐字节不变（Req 8.1，
        # Property 16「停用时逐字节不变」）。
        self._faithfulness = faithfulness_agent
        # 语言润色智能体（可选）：反馈循环收敛后、导出前运行一次独立语言 pass。
        # 缺省 None → 不运行，行为不变。Mock provider 装配时该 agent 自身 no-op。
        self._polish = language_polish_agent
        # 用户澄清问答器（human-in-the-loop）：缺省 AutoElicitor（非交互，取默认答案），
        # 使既有批处理/测试行为逐字节不变；CLI 交互模式注入 CLIElicitor。
        self._elicitor: Elicitor = elicitor or AutoElicitor()
        # 动态澄清问题提出器（路径 B，可选）：缺省 None → 不提动态问题，行为不变。
        # 仅在交互式 Elicitor 下触发（见 _llm_clarify）。
        self._clarify_proposer = clarification_proposer
        # 术语抽取智能体（可选）：语言润色前运行一次，填充 ws.glossary 供润色统一用词。
        # 缺省 None → 不运行；Mock provider 装配时该 agent 自身 no-op。
        self._terminology = terminology_agent
        # 被引文献正文抓取器（可选）：注入时在检索后富化 ref.full_text，供忠实性审计
        # 做正文级 grounding。缺省 None → 不富化，行为不变。
        self._full_text_fetcher = full_text_fetcher

    # --- 公开入口 ---

    def run(
        self,
        request: PaperRequest | None = None,
        resume_id: str | None = None,
    ) -> PaperResult:
        """运行完整工作流；预算计时覆盖初始化、全部阶段与收尾。"""
        budget = RunBudgetContext(
            token_cap=max(0, int(self._config.total_token_budget or 0)),
            duration_cap_s=max(
                0.0, float(getattr(self._config, "wall_clock_deadline_s", 0.0) or 0.0)
            ),
            call_cap=max(
                0, int(getattr(self._config, "total_llm_call_budget", 0) or 0)
            ),
        )
        token = activate_run_budget(budget)
        try:
            return self._run_impl(request=request, resume_id=resume_id)
        finally:
            reset_run_budget(token)

    def _run_impl(
        self,
        request: PaperRequest | None = None,
        resume_id: str | None = None,
    ) -> PaperResult:
        if resume_id is not None:
            ws = self._repo.load(resume_id)
            if ws is None:
                raise InputValidationError(f"找不到可续跑的工作区：{resume_id}")
            self._emit(EventKind.WORKFLOW_START, f"续跑工作区 {resume_id}（第 {ws.iteration} 轮后）")
            # Round 7：续跑时也检查 GENERATION 模式无 artifact。
            if (
                ws.input_mode is InputMode.GENERATION
                and (ws.artifact is None or ws.artifact.is_empty())
            ):
                self._emit(
                    EventKind.AGENT_LOG,
                    "[警告] GENERATION 模式无 artifact——产出为 LLM 推断版，"
                    "可能与作者实际研究不符。",
                )
        else:
            if request is None:
                raise InputValidationError("需提供 request 或 resume_id。")
            ws = self._init_workspace(request)
            self._emit(
                EventKind.WORKFLOW_START,
                f"模式={ws.input_mode.value}　输出={ws.output_format.value}",
            )

        # 各阶段按"是否已完成"跳过，实现断点续跑。
        # Round 7：GENERATION 模式无 artifact 时显式降级警告。
        if (
            ws.input_mode is InputMode.GENERATION
            and (ws.artifact is None or ws.artifact.is_empty())
        ):
            self._emit(
                EventKind.AGENT_LOG,
                "[警告] GENERATION 模式无 artifact——产出为 LLM 推断版，"
                "可能与作者实际研究不符。建议提供 ResearchArtifact 以 grounding 正文。",
            )
        if not ws.outline:
            self._plan_phase(ws)
        # 澄清阶段（草稿修订）：据初稿结构缺口就"范围/补章节"征询用户，
        # 决策写入 ws.profile 并按需补齐大纲章节。非交互下取默认（仅语言润色）→
        # 无结构改动、行为不变。已澄清过（续跑）则跳过。
        self._clarify_phase(ws)
        if (
            ws.input_mode is InputMode.DRAFT_REVISION
            and self._audit is not None
            and not ws.citation_audit
        ):
            self._audit_phase(ws)
        if self._needs_retrieval(ws) and not ws.retrieval_completed:
            self._retrieval_phase(ws)
        # 被引文献正文富化（可选）：在反馈循环前填充 ref.full_text，使循环内的
        # 忠实性审计能做正文级 grounding。未注入抓取器时整体跳过、行为不变。
        self._enrich_grounding_phase(ws)
        reason, unmet = self._feedback_loop(ws)
        # 术语抽取（可选）：语言润色前填充 ws.glossary，供润色统一用词。
        if not self._budget_exceeded() and self._has_optional_time(90):
            self._terminology_phase(ws)
        else:
            self._emit(EventKind.AGENT_LOG, "预算/截止时间已达，跳过可选术语抽取")
        # 语言润色（可选）：反馈循环收敛后、导出前运行一次独立语言 pass。
        if not self._budget_exceeded() and self._has_optional_time(180):
            self._polish_phase(ws)
        else:
            self._emit(EventKind.AGENT_LOG, "预算/截止时间已达，跳过可选语言润色")
        export = self._export_phase(ws, reason)
        # 原创性自检 + 可投递性判定（不改动工作区，不新增导出文件；结果并入
        # export.notes 与 PaperResult，并经事件上报）。
        originality_findings = self._originality_phase(ws)
        submittable, sub_notes = self._submittability_phase(
            ws, reason, export, originality_findings
        )
        self._emit(
            EventKind.WORKFLOW_END,
            f"原因={reason}　未达标维度={[d.value for d in unmet]}　"
            f"可投递={'是' if submittable else '否'}",
        )
        return PaperResult(
            workspace_id=ws.workspace_id,
            terminated_reason=reason,
            unmet_dimensions=unmet,
            export=export,
            submittable=submittable,
            submittability_notes=sub_notes,
        )

    # --- 各阶段 ---

    def _init_workspace(self, request: PaperRequest) -> PaperWorkspace:
        mode = self._detect_mode(request)
        ws = PaperWorkspace(
            workspace_id=uuid.uuid4().hex[:12],
            input_mode=mode,
            output_format=request.output_format
            or self._config.default_output_format,
            original_draft=request.draft,
            topic_background=request.topic_background,
        )
        if request.figures:
            ws.figures = list(request.figures)
        if request.profile:
            ws.profile = dict(request.profile)
        # venue 模板选择优先级：ws.profile["venue_id"] > config.venue_id > "default"。
        # 装配缺口修复：此前 config.venue_id 从不被消费（导出器只读 profile）。
        # 这里在 profile 未显式指定时把 config.venue_id 灌进 profile，打通中间一段。
        if not ws.profile.get("venue_id") and getattr(self._config, "venue_id", ""):
            ws.profile["venue_id"] = self._config.venue_id
        # 用户提供的会议样式文件目录：与 venue_id 注入同理，profile 未显式指定时
        # 把 config.styles_dir 灌进 profile，供导出器发现 .sty/.cls 文件。
        if not ws.profile.get("styles_dir") and getattr(self._config, "styles_dir", None):
            ws.profile["styles_dir"] = self._config.styles_dir
        # Round 7：注入用户提供的真实研究内容。
        if request.artifact is not None:
            ws.artifact = request.artifact
        return self._repo.create(ws)

    @staticmethod
    def _detect_mode(request: PaperRequest) -> InputMode:
        # Req 1.1 / 1.2 / 1.3
        if request.draft:
            return InputMode.DRAFT_REVISION
        if request.topic_background:
            return InputMode.GENERATION
        raise InputValidationError(
            "请求需包含已有初稿，或主题背景与实验数据之一。"
        )

    def _plan_phase(self, ws: PaperWorkspace) -> None:
        self._emit(EventKind.PHASE, "规划阶段")
        self._run_agent(ws, self._plan, "规划智能体分析主题、生成大纲与任务清单")

    # 常规章节体裁 → 新增大纲节点时的默认标题/摘要提示/相对位置权重。
    _NEW_SECTION_SPEC = {
        "introduction": ("引言", "研究背景、动机、现有工作不足与本文贡献。", -100.0),
        "related_work": ("相关工作", "按子主题归纳已有方法，并点明与本文的差异。", -50.0),
        "conclusion": ("结论", "总结本文贡献与主要结果，给出未来工作方向。", 100.0),
    }

    def _clarify_phase(self, ws: PaperWorkspace) -> None:
        """澄清阶段：一次性收集所有缺口问题 → ``ask_batch`` 一屏问完 → 落地。

        此前是「先问范围、再逐章问」的一步一停体验；现重写为：
        1. 用 ``draft_analyzer.analyze_draft`` 扫描章节/引用/数字/输出格式缺口；
        2. 用 ``clarification.collect_clarification_questions`` 收集成一批 Question；
        3. 经 ``Elicitor.ask_batch`` 一屏问完；
        4. 答案汇成 ``RevisionScope`` + 澄清偏好，写入 ``ws.profile``，下游据此行事。

        仅在尚未澄清（``ws.profile['clarified']`` 未置位）时运行，续跑不重复问。
        非交互（``AutoElicitor``）下所有问题取默认 → 范围「仅语言润色」、不改大纲、
        不补引用；行为逐字节不变（向后兼容）。无任何缺口时直接跳过、不向用户提问。
        """
        if ws.profile.get("clarified"):
            return  # 已澄清（含续跑）——不重复询问
        if ws.input_mode is InputMode.DRAFT_REVISION:
            self._clarify_scope(ws)
        self._llm_clarify(ws)
        self._apply(ws, _mark_clarified())

    def _clarify_scope(self, ws: PaperWorkspace) -> None:
        """确定性范围澄清（草稿修订）：一屏问完所有缺口 → 落地范围与补章节。"""
        from paper_agent.clarification import clarify_revision_scope_batch
        from paper_agent.draft_analyzer import analyze_draft

        gaps = analyze_draft(ws, input_ext=self._input_ext(ws))
        scope, preferences = clarify_revision_scope_batch(self._elicitor, gaps)

        # 记录范围决策与澄清偏好（可复现、续跑不重复）。
        self._apply(ws, _set_revision_scope(scope.to_dict()))
        if preferences:
            self._apply(ws, _set_clarification_preferences(preferences))

        # 用户选了改回与输入一致的输出格式 → 覆盖工作区输出格式。
        if (
            gaps.output_format_mismatch
            and preferences.get("output_format", "").startswith("改回与输入一致")
        ):
            new_fmt = self._input_ext_default_output(ws)
            if new_fmt is not None:
                self._apply(ws, _set_output_format(new_fmt))

        if not scope.sections_to_add:
            self._emit(
                EventKind.AGENT_LOG,
                "澄清：修订范围=仅语言润色"
                if not scope.add_citations
                else "澄清：修订范围=语言润色 + 补充文献",
            )
            return

        new_nodes = self._build_new_section_nodes(ws, scope.sections_to_add)
        if new_nodes:
            self._apply(ws, _add_outline_nodes(new_nodes))
            names = "、".join(n.title for n in new_nodes)
            self._emit(EventKind.AGENT_LOG, f"澄清：用户选择新增章节 {names}")

    @staticmethod
    def _input_ext(ws: PaperWorkspace) -> str:
        """从工作区推断输入文件扩展名（用于输出格式冲突检测）。"""
        # ws 不直接存输入路径；从 profile['input_path'] 取（由 CLI 在初始化时写入）。
        path = ws.profile.get("input_path") if ws.profile else None
        if not path:
            return ""
        import os

        return os.path.splitext(path)[1]

    @staticmethod
    def _input_ext_default_output(ws: PaperWorkspace):
        """输入扩展名对应的默认输出格式（用于「改回与输入一致」）。"""
        from paper_agent.entry import default_output_format

        ext = Orchestrator._input_ext(ws)
        if not ext:
            return None
        return default_output_format(ext)

    def _llm_clarify(self, ws: PaperWorkspace) -> None:
        """LLM 动态澄清（路径 B）：据场景提出至多 N 条问题，``ask_batch`` 一屏问完、记录。

        三重约束保证不失控：未注入提出器 / 非交互 Elicitor → 直接跳过（不花 LLM
        调用）；提出器自身限量且仅 PARSED 才产问题；答案记入 ``ws.profile``，写作
        阶段作为"用户澄清偏好"注入 prompt。

        此前是 ``for q: self._elicitor.ask(q)`` 一步一停；现改为 ``ask_batch`` 一屏
        问完，与确定性澄清的批量体验一致。
        """
        if self._clarify_proposer is None:
            return
        if not getattr(self._elicitor, "interactive", False):
            return
        try:
            questions = self._clarify_proposer.propose(ws)
        except Exception as exc:  # noqa: BLE001 - 提问失败不中止管线
            self._emit(EventKind.AGENT_LOG, f"动态澄清降级：{type(exc).__name__}")
            return
        if not questions:
            return
        answers_map = self._elicitor.ask_batch(questions)
        answers: list[dict] = []
        for q in questions:
            ans = answers_map.get(q.id, "")
            if ans and ans.strip():
                answers.append({"question": q.prompt, "answer": ans.strip()})
        if answers:
            self._apply(ws, _set_clarification_answers(answers))
            self._emit(EventKind.AGENT_LOG, f"澄清：采纳 {len(answers)} 条用户补充")

    def _build_new_section_nodes(
        self, ws: PaperWorkspace, section_type_values: list[str]
    ) -> list[OutlineNode]:
        """为选中的缺失章节体裁构造新的 ``OutlineNode``（去重、放置在合理位置）。"""
        existing_ids = {n.section_id for n in ws.outline}
        orders = [n.order for n in ws.outline] or [0]
        base_min, base_max = min(orders), max(orders)
        nodes: list[OutlineNode] = []
        for value in section_type_values:
            spec = self._NEW_SECTION_SPEC.get(value)
            if spec is None:
                continue
            title, hint, weight = spec
            section_id = value  # 体裁值本身即稳定 id（introduction/related_work/conclusion）
            if section_id in existing_ids:
                continue
            # 负权重置于最前、正权重置于最后；避免与既有 order 冲突。
            order = (base_min + weight) if weight < 0 else (base_max + weight)
            nodes.append(
                OutlineNode(
                    section_id=section_id,
                    title=title,
                    order=order,
                    summary_hint=hint,
                )
            )
            existing_ids.add(section_id)
        return nodes

    def _audit_phase(self, ws: PaperWorkspace) -> None:
        self._emit(EventKind.PHASE, "引用审计阶段")
        self._run_agent(ws, self._audit, "引用审计智能体核验初稿中的参考文献与引用")
        # 把审计发现逐条上报，便于用户在终端看到问题。
        for finding in ws.citation_audit:
            self._emit(
                EventKind.AGENT_LOG,
                f"[{finding.get('severity', '?')}] {finding.get('message', '')}",
            )

    @staticmethod
    def _needs_retrieval(ws: PaperWorkspace) -> bool:
        return any(t.needs_retrieval for t in ws.task_checklist)

    def _retrieval_phase(self, ws: PaperWorkspace) -> None:
        self._emit(EventKind.PHASE, "文献检索阶段")
        self._run_agent(ws, self._search, "检索智能体收集并核验文献")
        # #8：标记检索阶段已完成，避免续跑或「库里已有文献」时重复跳过。
        self._apply(ws, _set_retrieval_completed())

    def _feedback_loop(
        self, ws: PaperWorkspace
    ) -> tuple[str, list[ScoringDimension]]:
        """写作—评审反馈循环（Req 2 / Req 8）+ 确定性质量闸 + 停滞早退。

        "质量达标"需同时满足三项可观测条件（Req 2.1）：
        - 最近一条 ReviewRecord 的 ``parse_status == PARSED``（评审可信）；
        - ``unmet_dimensions`` 为空（全维度达标）；
        - 确定性质量闸通过（``report.passed``，无高严重度问题）。

        任一条件不成立都不返回 ``"quality_met"``（Req 2.2）；``ws.review_records``
        为空时不判达标（Req 2.3）。每轮 ``ws.iteration`` 恰增 1（Req 2.4），
        到达 ``iteration_limit`` 时按"最近评审是否可信"区分终止原因：
        不可信 → ``iteration_limit_unparsed_review``（Req 2.6），
        可信但未达标 → ``iteration_limit`` 并返回 ``unmet_dimensions``（Req 2.7）。
        迭代上限保证在 ``iteration_limit`` 轮内必然终止（Req 2.8）。

        #10 修复：进入每轮前先判上限——续跑到 ``iteration == limit`` 时直接终止，
        不再多跑一整轮写作+评审（修复 resume-at-limit 的 off-by-one）。
        #9 修复：仅当评审可信且内容签名连续 2 轮不变时提前以 ``"stagnation"``
        终止，避免对已不改进的草稿空转烧 token；不可信评审不触发，保留「跑满上限
        以可诊断原因终止」的语义。
        #19 修复：全局 token 预算超额时以 ``"budget_exceeded"`` 降级终止，直接导出，
        防止失控耗费。
        #7 修复：评审可信且「论证充分性」未达标时，回流触发一次补检索，使反馈循环
        能据评审缺引用信号补充文献（检索不再仅前置一次性）。
        """
        self._emit(EventKind.PHASE, "写作—评审反馈循环")
        # #7：续跑时据最近一条评审记录派生首轮修订目标，避免续跑第一轮用空 edits
        # 写作、浪费一轮且忽略已有反馈。新鲜运行（无章节/无评审）时返回 {}。
        edits = self._build_edits(
            ws, self._unmet_dimensions(ws), self._gate.check(ws)
        )
        prev_sig: str | None = None
        stagnation = 0
        retrieval_revisited = False
        while True:
            # 全局截止时间从 Orchestrator.run 入口计时，而非仅反馈循环。
            budget_reason = self._budget_reason()
            if budget_reason == "deadline":
                self._emit(
                    EventKind.AGENT_LOG,
                    f"墙钟超时（>{self._config.wall_clock_deadline_s}s），降级终止",
                )
                return "deadline_exceeded", self._unmet_dimensions(ws)
            # #10：循环顶判上限——续跑到上限时不再多跑一整轮。
            if ws.iteration >= self._config.iteration_limit:
                review_trustworthy = (
                    bool(ws.review_records)
                    and ws.review_records[-1].parse_status is ParseStatus.PARSED
                )
                reason = (
                    "iteration_limit_unparsed_review"
                    if not review_trustworthy
                    else "iteration_limit"
                )
                return reason, self._unmet_dimensions(ws)
            # #19：预算超额即降级终止（仍执行导出）。
            if budget_reason:
                self._emit(
                    EventKind.AGENT_LOG,
                    f"全局预算已达（{budget_reason}），降级终止",
                )
                return "budget_exceeded", self._unmet_dimensions(ws)
            self._emit(EventKind.ITERATION, f"第 {ws.iteration + 1} 轮")
            self._run_agent(
                ws, self._writing, "写作智能体撰写/修订章节", extras=edits
            )
            self._run_agent(ws, self._review, "评审智能体评分与反馈")
            # Round 4：每轮主审后立即跑对抗式评审（默认 reject 立场），与主审
            # 联合判定达标；二者均必须 PARSED 且通过才算 quality_met。
            if self._adversarial is not None:
                self._run_agent(
                    ws, self._adversarial, "对抗式评审：默认 reject 找 weakness"
                )
            # citation-faithfulness-audit（Req 6.2）：每轮评审（含对抗审）之后、
            # _build_edits 之前运行忠实性审计，使无支撑引用能在下一轮作为 gate_fixes
            # 驱动修订。未装配时整体跳过（Req 8.1）。
            self._faithfulness_phase(ws)
            self._apply(ws, _bump_iteration())  # 每轮恰增 1（Req 2.4）
            self._emit_scores(ws)
            self._emit_adversarial(ws)

            unmet = self._unmet_dimensions(ws)
            report = self._gate.check(ws)
            self._apply(ws, _set_quality_report(report.issues))
            self._emit_gate(report)

            # ★关键守卫：仅当最近一条评审被真实解析（PARSED）时才可触发达标。
            # review_records 为空（不应发生，评审已执行）时亦视为不可信（Req 2.3）。
            review_trustworthy = (
                bool(ws.review_records)
                and ws.review_records[-1].parse_status is ParseStatus.PARSED
            )
            llm_ok = review_trustworthy and not unmet
            gate_ok = report.passed
            # Round 4：对抗式评审通过判据——仅当装配了对抗审且最近一条 PARSED 且
            # decision == "accept" 才视为通过。未装配时不参与判定（向后兼容）。
            adversarial_ok = self._adversarial_ok(ws)
            # citation-faithfulness-audit（Req 6.3）：无 unsupported 发现（或未装配）
            # 才算引用忠实性达标；与既有三项 AND 合并。未装配时恒 True（Req 6.4/8.1）。
            faithfulness_ok = self._faithfulness_ok(ws)
            accuracy_ok = self._accuracy_ok(ws, report)
            if llm_ok and gate_ok and adversarial_ok and faithfulness_ok:
                return "quality_met", []  # 四方联合通过
            if accuracy_ok:
                return "accuracy_met", unmet

            # #7：评审可信但「论证充分性」不足（常意味着引用/论据不够）→ 回流补检索
            # 一次，使后续修订轮可引用新文献。仅触发一次，避免循环放大检索成本。
            if (
                review_trustworthy
                and ScoringDimension.SUFFICIENCY in unmet
                and not retrieval_revisited
            ):
                self._emit(
                    EventKind.PHASE,
                    "补检索：评审指出论证充分性不足，补充相关文献",
                )
                self._run_agent(ws, self._search, "检索智能体补充文献")
                retrieval_revisited = True

            # #9：停滞检测（仅可信评审）——内容签名连续 2 轮不变则提前结束。
            sig = _content_signature(ws)
            if review_trustworthy and prev_sig is not None and sig == prev_sig:
                stagnation += 1
            else:
                stagnation = 0
            prev_sig = sig
            if stagnation >= 2:
                self._emit(
                    EventKind.AGENT_LOG,
                    "检测到连续无进展，提前结束反馈循环以避免空转",
                )
                return "stagnation", unmet

            # 据未达标维度 + 质量闸问题构造下一轮的局部修订目标。
            edits = self._build_edits(ws, unmet, report)

    def _export_phase(self, ws: PaperWorkspace, reason: str = "") -> ExportResult:
        """导出阶段（Req 10.2 优雅降级）。

        无论反馈循环以何种原因终止都执行导出；即使以
        ``iteration_limit_unparsed_review`` 终止（最近评审不可信），仍使用工作区中
        最近一次成功写入/解析的章节草稿执行全部已配置的导出格式，不中止管线。
        """
        blockers = self._artifact_export_blockers(ws)
        if blockers:
            note = (
                "ArtifactCommitGate 未通过，已阻止正常论文导出；"
                f"请先修复 {len(blockers)} 个事实约束问题。"
            )
            self._emit(EventKind.AGENT_LOG, note)
            return ExportResult(
                output_format=ws.output_format,
                files=[],
                notes=[note, *[item.get("message", "") for item in blockers[:10]]],
            )
        if reason == "iteration_limit_unparsed_review":
            self._emit(
                EventKind.AGENT_LOG,
                "优雅降级：最近评审不可信，仍以最近一次成功解析的草稿执行导出",
            )
        self._emit(EventKind.PHASE, f"导出（{ws.output_format.value}）")
        exporter = get_exporter(ws.output_format)
        result = exporter.export(ws, self._config.workspace_dir)
        for f in result.files:
            self._emit(EventKind.AGENT_LOG, f"产出文件：{f}")
        # 任务 21.1：接入确定性格式闸 + 有界修复循环（Req 6.8/8.5/11.2-11.5/12.1）。
        result = self._run_format_gate(ws, exporter, result)
        return result

    def _artifact_export_blockers(self, ws: PaperWorkspace) -> list[dict]:
        artifact = ws.artifact
        if artifact is None or artifact.is_empty():
            return []
        from paper_agent.tools.artifact_commit_gate import ArtifactCommitGate

        gate = ArtifactCommitGate()
        blockers: list[dict] = []
        for node in ws.ordered_sections():
            draft = ws.section_drafts.get(node.section_id)
            strict = bool(
                node.required_evidence_ids
                or node.allowed_evidence_ids
                or (draft and (draft.artifact_hash or draft.evidence_ids))
            )
            if not strict:
                continue
            if draft is None:
                blockers.append({
                    "type": "empty_section",
                    "severity": "high",
                    "section_id": node.section_id,
                    "message": f"章节《{node.title}》尚无通过事实门禁的正文。",
                })
                continue
            blockers.extend(gate.check(ws, node, draft).high_violations)
        return blockers

    def _run_format_gate(
        self, ws: PaperWorkspace, exporter, result: ExportResult
    ) -> ExportResult:
        """在导出产物上运行格式闸并按需修复；全程绝不中止管线（Req 11.2）。

        - 未注入格式闸，或输出非 LaTeX/docx（如 Markdown）→ 原样返回（Req 7 语义）。
        - 闸通过 → 记一条通过日志后返回。
        - 缺工具（pandoc/pdflatex 不可用）→ 不跑修复（LLM 无法装工具），降级标注
          并返回（Req 8.5 隔离 / 11.2 不中止）。
        - 内容/编译失败且注入了修复循环 → 有界修复；写回经 WorkspaceRepository
          （Req 12.1）；修复成功记日志，耗尽则降级标注最近产物（Req 11.2/11.3/11.4）；
          工作区保留最后一次成功写回的章节内容（Req 11.5）。

        任何闸/修复内部异常都降级为「返回原始导出产物 + 一条说明」，不冒泡中止管线。
        """
        if self._format_gate is None:
            return result
        if ws.output_format not in (OutputFormat.LATEX, OutputFormat.DOCX):
            return result  # Markdown 从不经闸

        try:
            report = self._format_gate.check(ws.output_format, result.files)

            if report.passed:
                self._emit(EventKind.AGENT_LOG, "格式闸通过")
                return result

            # 缺工具：LLM 无法安装外部工具，跳过修复，优雅降级（Req 8.5 / 11.2）。
            if report.missing_tools:
                tools = "、".join(report.missing_tools)
                note = f"格式未校验：缺少工具 {tools}"
                result.notes.append(note)
                self._emit(
                    EventKind.DEGRADATION,
                    note,
                    feature="format_gate",
                    reason="missing_tools",
                    missing_tools=list(report.missing_tools),
                    output_format=ws.output_format.value,
                )
                return result

            # 内容/编译失败：注入了修复循环则有界修复；否则仅降级标注。
            if self._format_repair_loop is None:
                note = "格式未通过：未配置修复循环"
                result.notes.append(note)
                self._emit(
                    EventKind.DEGRADATION,
                    note,
                    feature="format_repair",
                    reason="no_repair_loop",
                    output_format=ws.output_format.value,
                )
                return result

            return self._repair_and_reexport(ws, exporter, result, report)
        except Exception as exc:  # noqa: BLE001 - 闸/修复内部异常绝不中止管线
            note = f"格式校验降级：内部错误（{type(exc).__name__}）"
            result.notes.append(note)
            self._emit(
                EventKind.DEGRADATION,
                note,
                feature="format_gate",
                reason="internal_error",
                output_format=ws.output_format.value,
            )
            return result

    def _repair_and_reexport(
        self,
        ws: PaperWorkspace,
        exporter,
        result: ExportResult,
        report: FormatGateReport,
    ) -> ExportResult:
        """运行有界修复循环并落盘其写回，随后按需重导出（Req 11.2-11.5/12.1）。"""
        out_dir = self._config.workspace_dir
        outcome = self._format_repair_loop.run(ws, report, exporter, out_dir)

        # 写回经既有单一写路径（WorkspaceRepository），保证原子落盘（Req 12.1）。
        if outcome.mutations:
            self._apply(ws, AgentResult(mutations=outcome.mutations))
            # 落盘后重新导出一次，得到反映修复结果的最终产物。
            result = exporter.export(ws, out_dir)
            for f in result.files:
                self._emit(EventKind.AGENT_LOG, f"产出文件（修复后）：{f}")

        if outcome.status == RepairTerminalStatus.REPAIRED_WITHIN_LIMIT:
            self._emit(
                EventKind.AGENT_LOG,
                f"格式修复成功：在 {len(outcome.attempts)} 次尝试内通过格式闸",
            )
            return result

        # REPAIR_EXHAUSTED：不中止管线，输出最近一次产物 + 一致措辞标注（Req 11.3）。
        excerpt = self._last_tool_error(outcome.last_report)
        note = "格式未通过：已达修复上限"
        if excerpt:
            note = f"{note}；最后错误：{excerpt}"
        result.notes.append(note[:2000])
        self._emit(
            EventKind.DEGRADATION,
            "格式未通过：已达修复上限",
            feature="format_repair",
            reason="repair_exhausted",
            output_format=ws.output_format.value,
            attempts=len(outcome.attempts),
            tool_runs=outcome.tool_runs,
            last_error=excerpt[:2000],
        )
        return result

    @staticmethod
    def _last_tool_error(report: FormatGateReport | None) -> str:
        """从（最近一次）格式闸报告汇聚一段 ≤2000 字符的错误摘要（Req 11.3）。"""
        if report is None:
            return ""
        parts: list[str] = []
        for t in getattr(report, "tool_results", None) or []:
            name = getattr(t, "tool_name", "") or "tool"
            code = getattr(t, "exit_code", None)
            stderr = getattr(t, "stderr_excerpt", "") or ""
            if getattr(t, "timed_out", False):
                parts.append(f"[{name}] 超时：{stderr}")
            elif getattr(t, "missing", False):
                parts.append(f"[{name}] 缺失：{stderr}")
            elif code not in (0, None):
                parts.append(f"[{name}] 退出码 {code}：{stderr}")
        text = "\n".join(p for p in parts if p)
        return text[:2000]

    # --- 辅助 ---

    def _unmet_dimensions(self, ws: PaperWorkspace) -> list[ScoringDimension]:
        if not ws.review_records:
            return list(ScoringDimension)
        latest = ws.review_records[-1]
        unmet = []
        for dim in ScoringDimension:
            score = latest.scores.get(dim, 0.0)
            if score < self._config.threshold_for(dim):
                unmet.append(dim)
        return unmet

    @staticmethod
    def _build_edits(
        ws: PaperWorkspace, unmet: list[ScoringDimension], report=None
    ) -> dict:
        """据评审结果 + 质量闸问题 + 对抗式 weakness 生成局部修订目标。

        #11：区分两类来源，避免主观建议与客观硬错误混进同一段无标号文字：
        - ``gate_fixes``（硬·客观）：质量闸**高严重度**问题，必须修复；
          Round 4：对抗式评审 ``severity == "critical"`` 的 weakness 也归此类。
        - ``edits``（软·主观/中低严重度）：评审章节级反馈 + 质量闸中低严重度问题
          + 对抗式评审的 major/minor weakness。
        写作智能体据此按来源标注、优先处理硬修复。

        #4：无任何可执行反馈时返回空 dict——不再回退「改最短章节」。原回退只会
        对与真因无关的章节无意义重写、空转烧 token（尤其评审解析失败时）。
        """
        if not ws.section_drafts:
            return {}
        edits: dict[str, str] = {}
        gate_fixes: dict[str, list[str]] = {}

        # 1) 质量闸问题：高严重度 → 硬修复；中低严重度 → 软建议。
        if report is not None:
            for issue in report.issues:
                sid = issue.get("section_id")
                if not sid or sid not in ws.section_drafts:
                    continue
                msg = issue.get("message", "")
                if issue.get("severity") == "high":
                    gate_fixes.setdefault(sid, []).append(msg)
                else:
                    edits[sid] = (edits.get(sid, "") + " " + msg).strip()

        # 2) 评审给出的章节级反馈（软建议）。
        latest = ws.review_records[-1] if ws.review_records else None
        dim_suggestion = ""
        if latest is not None:
            dim_suggestion = "；".join(
                latest.suggestions.get(dim, "") for dim in unmet
            ).strip("；")
            for sid, feedback in latest.section_feedback.items():
                if sid in ws.section_drafts:
                    extra = f"{feedback}。整体方向：{dim_suggestion}" if dim_suggestion else feedback
                    edits[sid] = (edits.get(sid, "") + " " + extra).strip()

        # 3) 对抗式评审 weakness（Round 4）：critical → 硬修复；其余 → 软建议。
        #    section_id 不在草稿中（如对抗审用了标题或留空）的归到「找不到归属」
        #    的全局软建议，分摊到任一存在的章节，避免丢失关键信号。
        adversarial = (
            ws.adversarial_records[-1] if ws.adversarial_records else None
        )
        if adversarial is not None and adversarial.parse_status is ParseStatus.PARSED:
            title_to_id = {n.title: n.section_id for n in ws.outline}
            for w in adversarial.weaknesses:
                sid = w.get("section_id", "")
                # 章节标题 → section_id 归一。
                if sid not in ws.section_drafts and sid in title_to_id:
                    sid = title_to_id[sid]
                # 仍找不到归属：分摊到最长章节，避免完全丢弃 critical 信号。
                if sid not in ws.section_drafts:
                    if not ws.section_drafts:
                        continue
                    sid = max(
                        ws.section_drafts,
                        key=lambda s: len(ws.section_drafts[s].content),
                    )
                category = w.get("category", "other")
                issue_text = w.get("issue", "")
                fix_text = w.get("suggested_fix", "")
                msg = f"[对抗·{category}] {issue_text}"
                if fix_text:
                    msg += f"（修复建议：{fix_text}）"
                if w.get("severity") == "critical":
                    gate_fixes.setdefault(sid, []).append(msg)
                else:
                    edits[sid] = (edits.get(sid, "") + " " + msg).strip()

        # 4) 引用忠实性审计（citation-faithfulness-audit Req 6.1）：把 verdict
        #    为 unsupported 的发现按 section_id 并入 gate_fixes（硬·客观，须修复/删除
        #    无支撑引用）。复用既有 setdefault(...).append(...) 路径。默认关闭 / 无发现
        #    （空列表）时不产生任何条目，输出逐字节不变（Property 16）。
        for finding in ws.citation_faithfulness:
            if finding.get("verdict") != "unsupported":
                continue
            sid = finding.get("section_id")
            if not sid or sid not in ws.section_drafts:
                continue
            ref_id = finding.get("cited_reference_id", "")
            rationale = (finding.get("rationale", "") or "")[:200]
            msg = (
                f"[忠实性·无支撑] 引用 [{ref_id}] 无法由该文献支撑其声明句，"
                f"请修正声明或删除该引用。理由：{rationale}"
            )
            gate_fixes.setdefault(sid, []).append(msg)

        # #4：无可执行反馈则不构造修订目标（不回退「改最短章节」）。
        if not edits and not gate_fixes:
            return {}
        return {"edits": edits, "gate_fixes": gate_fixes}

    def _enrich_grounding_phase(self, ws: PaperWorkspace) -> None:
        """被引文献正文富化阶段（可选）：填充 ref.full_text 供正文级 grounding。

        未注入抓取器 / 无已验证文献时整体跳过。经既有单一写入路径落盘；任何内部
        异常都降级为「不富化」，绝不中止管线。
        """
        if self._full_text_fetcher is None or not ws.verified_references:
            return
        from paper_agent.tools.faithfulness_extract import cited_reference_ids
        from paper_agent.tools.reference_enrichment import collect_full_texts

        self._emit(EventKind.PHASE, "文献正文富化阶段")
        cited_ids: set[str] = set()
        for draft in ws.section_drafts.values():
            cited_ids.update(cited_reference_ids(getattr(draft, "content", "") or ""))
        try:
            collected = collect_full_texts(
                ws.verified_references,
                self._full_text_fetcher,
                max_refs=getattr(self._config, "grounding_fulltext_max_refs", 20),
                cited_ids=cited_ids or None,
            )
        except Exception as exc:  # noqa: BLE001 - 富化异常不中止管线
            self._emit(EventKind.AGENT_LOG, f"正文富化降级：{type(exc).__name__}")
            return
        if not collected:
            return
        self._apply(ws, _set_reference_full_texts(collected))
        self._emit(
            EventKind.AGENT_LOG,
            f"正文富化：{len(collected)} 篇被引文献已取正文用于 grounding",
        )

    def _terminology_phase(self, ws: PaperWorkspace) -> None:
        """术语抽取阶段（可选）：语言润色前填充 ws.glossary。

        未注入术语智能体时整体跳过；经既有 ``_run_agent`` 单一写入路径运行。
        Mock provider 装配的术语智能体自身 no-op，故输出逐字节不变。
        """
        if self._terminology is None:
            return
        self._emit(EventKind.PHASE, "术语抽取阶段")
        self._run_agent(ws, self._terminology, "术语抽取：构建全篇统一术语表")

    def _polish_phase(self, ws: PaperWorkspace) -> None:
        """语言润色阶段（可选）：反馈循环收敛后、导出前运行一次。

        未注入润色智能体时整体跳过；经既有 ``_run_agent`` 单一写入路径运行，
        润色结果（章节正文改写）写回工作区。Mock provider 装配的润色智能体自身
        no-op，故输出逐字节不变。
        """
        if self._polish is None:
            return
        before = deepcopy(ws.section_drafts)
        self._emit(EventKind.PHASE, "语言润色阶段")
        self._run_agent(ws, self._polish, "语言润色：逐章节语言与一致性校对")
        blockers = self._artifact_export_blockers(ws)
        if blockers:
            changed = {
                sid
                for sid, draft in ws.section_drafts.items()
                if sid in before and draft.content != before[sid].content
            }
            if changed:
                def rollback(w: PaperWorkspace) -> None:
                    for sid in changed:
                        w.section_drafts[sid] = before[sid]
                    w.artifact_violations.extend(blockers)

                self._apply(ws, AgentResult(mutations=[rollback]))
                self._emit(
                    EventKind.AGENT_LOG,
                    f"润色后事实复检失败，已回滚 {len(changed)} 个章节",
                )

        def clear_modified_sections(w: PaperWorkspace) -> None:
            w.profile["modified_section_ids"] = []

        self._repo.update(ws, clear_modified_sections)

    def _originality_phase(self, ws: PaperWorkspace) -> list[dict]:
        """原创性 / 相似度自检（可选，确定性，不改动工作区）。

        据 ``config.originality_check_enabled`` 开关；对每章做与已核验文献的
        n-gram 重合度自检，把 findings 经事件上报并返回供可投递性判定。关闭或
        无发现时返回空列表。任何内部异常都降级为「返回空列表」，绝不中止管线。
        """
        if not getattr(self._config, "originality_check_enabled", False):
            return []
        try:
            from paper_agent.tools.originality_check import check_originality

            findings = check_originality(
                ws,
                n=self._config.originality_ngram,
                threshold=self._config.originality_overlap_threshold,
            )
        except Exception as exc:  # noqa: BLE001 - 自检异常不中止管线
            self._emit(EventKind.AGENT_LOG, f"原创性自检降级：{type(exc).__name__}")
            return []
        for f in findings:
            self._emit(
                EventKind.AGENT_LOG,
                f"[原创性·{f.get('severity')}] {f.get('message')}",
            )
        return findings

    def _submittability_phase(
        self,
        ws: PaperWorkspace,
        reason: str,
        export: ExportResult | None,
        originality_findings: list[dict],
    ) -> tuple[bool, list[str]]:
        """综合判定可投递性；把说明并入 ``export.notes`` 并经事件上报。

        不改动工作区、不新增导出文件；仅向既有 ``ExportResult.notes`` 追加人类
        可读说明。任何内部异常都降级为「视为可投递、返回空说明」，不中止管线。
        """
        try:
            from paper_agent.workspace.submittability import assess_submittability

            verdict = assess_submittability(
                ws,
                terminated_reason=reason,
                export_notes=list(export.notes) if export else [],
                originality_findings=originality_findings,
            )
        except Exception as exc:  # noqa: BLE001 - 判定异常不中止管线
            self._emit(EventKind.AGENT_LOG, f"可投递性判定降级：{type(exc).__name__}")
            return True, []
        notes = verdict.notes
        if export is not None:
            export.notes.extend(notes)
        for line in notes:
            self._emit(EventKind.AGENT_LOG, line)
        return verdict.submittable, notes

    def _faithfulness_phase(self, ws: PaperWorkspace) -> None:
        """引用忠实性审计阶段（citation-faithfulness-audit Req 6.2）。

        仅当装配了忠实性审计智能体时执行；经既有 ``_run_agent`` 单一写入路径运行，
        审计结果写入 ``ws.citation_faithfulness``（替换而非累加）。未装配时整体跳过，
        系统行为逐字节不变（Req 8.1）。
        """
        if self._faithfulness is None:
            return
        self._emit(EventKind.PHASE, "引用忠实性审计阶段")
        self._run_agent(
            ws, self._faithfulness, "忠实性审计：声明级 grounded 引用校验"
        )

    def _faithfulness_ok(self, ws: PaperWorkspace) -> bool:
        """引用忠实性达标判据（citation-faithfulness-audit Req 6.3/6.4/8.1）。

        未装配忠实性审计 → 视为达标（不参与判定，向后兼容，Req 8.1）；
        装配时，当且仅当没有任何 ``verdict == "unsupported"`` 的发现才算达标。
        """
        if self._faithfulness is None:
            return True
        return not any(
            f.get("verdict") == "unsupported" for f in ws.citation_faithfulness
        )

    def _accuracy_ok(self, ws: PaperWorkspace, report) -> bool:
        """准确性硬约束达标：无 Agent 伪造引用/事实违规，忠实性无 unsupported。

        不要求评审高分、对抗审 accept 或 gate 全绿；原稿遗留的
        ``source_citation_unverified`` 不阻断 accuracy_met。
        """
        if not self._faithfulness_ok(ws):
            return False
        skip_types = {"source_citation_unverified"}
        for issue in report.high_issues:
            if issue.get("type") in skip_types:
                continue
            return False
        if ws.artifact_violations:
            return False
        return True

    def _adversarial_ok(self, ws: PaperWorkspace) -> bool:
        """对抗式评审通过判据（Round 4）。

        未装配对抗审 → 视为通过（不参与判定，向后兼容）。
        装配但最近一条不可信（FAILED/MOCK_FALLBACK）→ 不通过。
        装配且 PARSED 时，``decision == "accept"`` 才通过——只要还有任何 weakness
        都不算通过（``AdversarialReviewAgent._build_record_from`` 已兜底：有 weakness
        时 decision 至少为 borderline，故"通过"必意味着模型确实找不到弱点）。
        """
        if self._adversarial is None:
            return True
        if not ws.adversarial_records:
            return False
        latest = ws.adversarial_records[-1]
        if latest.parse_status is not ParseStatus.PARSED:
            return False
        return latest.decision == "accept"

    def _emit_adversarial(self, ws: PaperWorkspace) -> None:
        if not ws.adversarial_records:
            return
        latest = ws.adversarial_records[-1]
        self._emit(
            EventKind.AGENT_LOG,
            f"对抗式评审：decision={latest.decision}，"
            f"weakness={len(latest.weaknesses)} 条（critical={latest.critical_count}）",
        )

    def _apply(self, ws: PaperWorkspace, result) -> None:
        """将智能体返回的更新意图逐个原子落盘。"""
        for mutation in result.mutations:
            self._repo.update(ws, mutation)

    def _run_agent(
        self, ws: PaperWorkspace, agent: Agent, label: str, extras: dict | None = None
    ) -> None:
        """运行一个智能体：发出开始事件、转发其日志、原子应用更新。

        #15：在智能体执行前后触发 ``Hooks`` 扩展点（审计/限流等）。
        """
        ctx = AgentContext(workspace=ws, extras=extras or {})
        before_writing = (
            {sid: draft.content for sid, draft in ws.section_drafts.items()}
            if agent is self._writing
            else None
        )
        try:
            self._check_run_budget()
            self._hooks.before_agent(agent.name, ctx)
            self._emit(EventKind.AGENT_START, label)
            result = agent.run(ctx)
        except BudgetExceededError as exc:
            self._emit(
                EventKind.DEGRADATION,
                f"{label} 在调用前因预算停止（{exc.reason}）",
                feature="run_budget",
                reason=exc.reason,
            )
            return
        self._hooks.after_agent(agent.name, ctx, result)
        for log in result.logs:
            self._emit(EventKind.AGENT_LOG, log)
        self._apply(ws, result)
        if before_writing is not None:
            modified = set(ws.profile.get("modified_section_ids") or [])
            for sid, draft in ws.section_drafts.items():
                previous = before_writing.get(sid)
                if previous == draft.content:
                    continue
                if (
                    previous is None
                    and ws.input_mode is InputMode.DRAFT_REVISION
                    and draft.content == ws.draft_sections.get(sid, "")
                ):
                    continue
                modified.add(sid)
            modified_ids = sorted(modified)

            def persist_modified_sections(w: PaperWorkspace) -> None:
                w.profile["modified_section_ids"] = modified_ids

            self._repo.update(ws, persist_modified_sections)

    def _budget_exceeded(self) -> bool:
        return bool(self._budget_reason())

    @staticmethod
    def _has_optional_time(reserve_s: float) -> bool:
        context = current_run_budget()
        return context is None or context.remaining_s >= reserve_s

    def _check_run_budget(self) -> None:
        context = current_run_budget()
        if context is None:
            return
        context.check(
            total_tokens=self._tracker.total_tokens if self._tracker else 0,
            calls=self._tracker.calls if self._tracker else 0,
        )

    def _budget_reason(self) -> str:
        """返回当前硬预算原因；检查本身不让受控异常逃出编排器。"""
        context = current_run_budget()
        if context is None:
            return ""
        try:
            self._check_run_budget()
        except BudgetExceededError as exc:
            return exc.reason
        return ""

    def _deadline_exceeded(self, start_time: float) -> bool:
        """墙钟是否超时（``wall_clock_deadline_s <= 0`` 表示不限）。"""
        limit = getattr(self._config, "wall_clock_deadline_s", 0.0) or 0.0
        return limit > 0 and (time.monotonic() - start_time) >= limit

    def _emit(self, kind: EventKind, message: str = "", **data) -> None:
        self._sink.emit(Event(kind=kind, message=message, data=data))

    def _emit_scores(self, ws: PaperWorkspace) -> None:
        if not ws.review_records:
            return
        latest = ws.review_records[-1]
        summary = "　".join(
            f"{dim.value}={latest.scores.get(dim, 0):.1f}" for dim in ScoringDimension
        )
        self._emit(EventKind.REVIEW_SCORES, f"评分　{summary}")

    def _emit_gate(self, report) -> None:
        if report.passed and not report.issues:
            self._emit(EventKind.AGENT_LOG, "质量闸：通过（无问题）")
            return
        status = "通过" if report.passed else "未通过（含高严重度问题）"
        self._emit(EventKind.AGENT_LOG, f"质量闸：{status}，共 {len(report.issues)} 项")
        for issue in report.issues:
            self._emit(
                EventKind.AGENT_LOG,
                f"  [{issue.get('severity')}] {issue.get('message')}",
            )


def _bump_iteration():
    """生成一个递增 iteration 的更新结果。"""

    def mutate(w: PaperWorkspace) -> None:
        w.iteration += 1

    return AgentResult(mutations=[mutate])


def _set_quality_report(issues: list[dict]):
    """生成一个写入质量报告的更新结果。"""

    def mutate(w: PaperWorkspace) -> None:
        w.quality_report = issues

    return AgentResult(mutations=[mutate])


def _set_retrieval_completed():
    """生成一个标记检索阶段完成的更新结果（#8）。"""

    def mutate(w: PaperWorkspace) -> None:
        w.retrieval_completed = True

    return AgentResult(mutations=[mutate])


def _set_reference_full_texts(mapping: dict[str, str]):
    """生成一个把富化到的被引文献正文写入对应 ReferenceEntry.full_text 的更新结果。"""

    def mutate(w: PaperWorkspace) -> None:
        for ref in w.verified_references:
            if ref.id in mapping:
                ref.full_text = mapping[ref.id]

    return AgentResult(mutations=[mutate])


def _set_revision_scope(scope: dict):
    """生成一个把澄清所得修订范围写入 profile 的更新结果。"""

    def mutate(w: PaperWorkspace) -> None:
        w.profile["revision_scope"] = scope

    return AgentResult(mutations=[mutate])


def _mark_clarified():
    """生成一个标记澄清阶段已完成的更新结果（续跑不重复问）。"""

    def mutate(w: PaperWorkspace) -> None:
        w.profile["clarified"] = True

    return AgentResult(mutations=[mutate])


def _set_clarification_answers(answers: list[dict]):
    """生成一个把 LLM 动态澄清答案写入 profile 的更新结果。"""

    def mutate(w: PaperWorkspace) -> None:
        w.profile["clarification_answers"] = list(answers)

    return AgentResult(mutations=[mutate])


def _set_clarification_preferences(preferences: dict[str, str]):
    """生成一个把一次性澄清的偏好（缺引用/数字/输出格式）写入 profile 的更新结果。"""

    def mutate(w: PaperWorkspace) -> None:
        existing = dict(w.profile.get("clarification_preferences") or {})
        existing.update(preferences)
        w.profile["clarification_preferences"] = existing

    return AgentResult(mutations=[mutate])


def _set_output_format(fmt):
    """生成一个覆盖工作区输出格式的更新结果（澄清阶段改回与输入一致时用）。"""

    def mutate(w: PaperWorkspace) -> None:
        w.output_format = fmt

    return AgentResult(mutations=[mutate])


def _add_outline_nodes(nodes: list[OutlineNode]):
    """生成一个把新章节节点追加进大纲的更新结果（澄清补章节）。"""

    def mutate(w: PaperWorkspace) -> None:
        existing = {n.section_id for n in w.outline}
        for node in nodes:
            if node.section_id not in existing:
                w.outline.append(node)
                existing.add(node.section_id)

    return AgentResult(mutations=[mutate])


def _content_signature(ws: PaperWorkspace) -> str:
    """工作区全部章节正文的稳定签名，用于停滞检测（#9）。

    按 section_id 排序拼接，使章节顺序无关；只含正文内容，不含元数据，
    故「内容不变」即可判定本轮写作未带来实质改变。
    """
    return "\n".join(
        f"{sid}:{ws.section_drafts[sid].content}"
        for sid in sorted(ws.section_drafts)
    )
