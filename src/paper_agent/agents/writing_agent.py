"""写作智能体（Req 5）。

两种工作方式：
1. 初次生成：工作区无任何章节草稿时，依据大纲逐章节撰写（Req 5.1），
   并为每节生成摘要（Req 5.2），写作时注入全局上下文（Req 5.3/5.5）。
2. 局部修改（Localized_Edit，Req 5.7-5.9）：已存在草稿时，
   仅修改评审建议指向的章节，未涉及章节的内容保持不变（Property 5）。
   - 内容型修订：替换目标章节内容。
   - 结构型修订：在受影响章节范围内新增/删除章节，不动其他章节。

写作时只引用已验证文献库中的条目（Req 4.3）。
"""

from __future__ import annotations

import json
import os

from paper_agent.agents.base import Agent, AgentContext, AgentResult
from paper_agent.agents.revision_types import FallbackReason, RevisionRoute
from paper_agent.agents.tool_loop import ToolLoopConfig, run_tool_loop
from paper_agent.context.manager import ContextManager
from paper_agent.context.tokenizer import TokenCounter, build_token_counter
from paper_agent.export import content_contract
from paper_agent.export.figure_renderer import FigureRenderer, RenderedFigure
from paper_agent.export.grounding import GroundingChecker
from paper_agent.export.plotting import MatplotlibBackend
from paper_agent.observability.events import Event, EventKind, EventSink, NullSink
from paper_agent.prompts import templates
from paper_agent.providers.llm.base import LLMProvider
from paper_agent.providers.retrieval.base import RetrievalProvider
from paper_agent.tools.citation import CitationVerifier
from paper_agent.tools.artifact_commit_gate import (
    ArtifactCommitGate,
    build_claim_manifest,
)
from paper_agent.tools.literature_tool import (
    _SEARCH_SCHEMA,
    LiteratureSearchTool,
    build_writing_tools,
)
from paper_agent.tools.quality_tools import QualityCheckTools
from paper_agent.tools.registry import ToolRegistry
from paper_agent.tools.ask_user_tool import AskUserTool, register_ask_user_tool
from paper_agent.tools.section_edit_tool import _EDIT_SCHEMA, SectionEditTool
from paper_agent.tools.workspace_tools import WorkspaceReadTools, WorkspaceView
from paper_agent.workspace.models import (
    FigureRecord,
    InputMode,
    OutlineNode,
    PaperWorkspace,
    SectionDraft,
    SectionEdit,
)

# function calling schema（名称 + 参数字段及类型）——读取/无参工具。
# edit_section / search_literature 复用各自工具模块中的权威 schema，避免漂移。
_READ_SECTION_SCHEMA = {
    "type": "object",
    "properties": {
        "section_id": {
            "type": "string",
            "description": "要读取全文的目标章节 id",
        }
    },
    "required": ["section_id"],
}
_READ_REFERENCE_SCHEMA = {
    "type": "object",
    "properties": {
        "reference_id": {
            "type": "string",
            "description": "要读取完整元数据的参考文献 id",
        }
    },
    "required": ["reference_id"],
}
_NO_PARAMS_SCHEMA = {"type": "object", "properties": {}}

# format-pipeline-and-diff-revision 11.1：修订可观测载荷中任何正文片段的长度上限
# （Req 4.4）。事件载荷仅承载 section_id / 路径 / 计数 / 回退原因等结构化字段，
# 绝不含 API 密钥或完整请求体；如需附带文本片段，一律经此上限截断。
_OBS_EXCERPT_MAX = 2000


class WritingAgent(Agent):
    name = "writing_agent"

    def __init__(
        self,
        llm: LLMProvider,
        context: ContextManager | None = None,
        retrieval: RetrievalProvider | None = None,
        verifier: CitationVerifier | None = None,
        max_tool_iters: int = 4,
        counter: TokenCounter | None = None,
        hooks=None,
        *,
        figure_renderer: FigureRenderer | None = None,
        figures_from_data_enabled: bool = True,
        workspace_dir: str | None = None,
        sink: EventSink | None = None,
        patch_first_enabled: bool = True,
        patch_size_limit: float = 0.5,
        elicitor=None,
        ask_user_budget: int = 3,
    ) -> None:
        self._llm = llm
        self._ctx = context or ContextManager(llm)
        # 同时提供检索与核验时，启用"写作期按需检索"的工具循环（Req 4.4）。
        self._retrieval = retrieval
        self._verifier = verifier
        self._max_tool_iters = max_tool_iters
        # 真实 token 计量器：注入升级后的工具循环以做历史压缩/结果截断（Req 7.6/8）。
        # 缺省构造统一计数器，保证既有 WritingAgent(llm, context, ...) 构造不回归。
        self._counter = counter or build_token_counter()
        # #15：工具调用扩展点（由 Orchestrator 注入，写进各 ToolRegistry）。
        self._hooks = hooks
        # venue-templates-figures-tables #15.1：数据出图接入点（Req 7.3 / 9.1 / 9.4）。
        # 全部为带默认值的关键字参数，保证既有构造不回归：
        # - figure_renderer：可注入的 FigureRenderer；None 时按需惰性构造默认实例。
        # - figures_from_data_enabled：数据出图开关（Req 7.6）；关闭时不产图，回落文字图题。
        # - workspace_dir：资产落盘根目录；None 时优雅跳过数据出图（缺路径不报错）。
        # - sink：事件接收器；None 时用 NullSink（无可观测开销）。
        self._figure_renderer = figure_renderer
        self._figures_from_data_enabled = figures_from_data_enabled
        self._workspace_dir = workspace_dir
        self._sink = sink
        # format-pipeline-and-diff-revision Part A：补丁优先修订路由配置。
        # - patch_first_enabled：内容型修订默认走 Patch_Mode（Req 1.1/1.7）；
        #   关闭时退化为整章重写（向后兼容旧行为）。
        # - patch_size_limit：补丁累计影响占比阈值（Req 3.2，取值 0.0–1.0），
        #   运行期回退判据在后续任务（8.1）落地，此处存储供路由使用。
        # 均为带默认值的关键字参数，保证既有 WritingAgent(...) 构造不回归。
        self._patch_first_enabled = patch_first_enabled
        self._patch_size_limit = patch_size_limit
        # 写作期 ask_user（mid-loop human-in-the-loop）：仅当注入了交互式 Elicitor 时
        # 才向模型暴露 ask_user 工具。非交互（None / AutoElicitor）→ 不注册、零影响。
        # 每次 run() 构造一个新的 AskUserTool（种子取自 ws.profile），配额防写作期狂问。
        self._elicitor = elicitor
        self._ask_user_budget = ask_user_budget
        self._ask_tool: AskUserTool | None = None
        # format-pipeline-and-diff-revision 8.1：暴露「补丁累计影响超过
        # patch_size_limit 而被判定应整章重写」的章节集合（Req 3.2）。由
        # `_materialize_edits` 在每次调用开始清空、发生超限时写入；供调用方
        # （既有修订路由）后续可选择对这些章节触发 Whole_Section_Regeneration，
        # 不改变 `_materialize_edits` 的既有返回契约（非破坏式暴露决策）。
        self._patch_size_exceeded_sections: set[str] = set()

    @property
    def _tool_mode(self) -> bool:
        return self._retrieval is not None and self._verifier is not None

    def run(self, ctx: AgentContext) -> AgentResult:
        ws = ctx.workspace
        self._gate_violations: list[dict] = []
        # 写作期 ask_user 工具：仅交互式 Elicitor 才启用（否则 None → 不注册）。
        # 每轮运行构造一个，种子取自已持久化的问答（续跑回放、不重复问）。
        self._ask_tool = self._make_ask_tool(ws)
        # 续跑安全：只要还有大纲章节未生成草稿，就先补齐这些章节；
        # 全部写完后才进入局部修订。
        missing = [
            n for n in ws.ordered_sections() if n.section_id not in ws.section_drafts
        ]
        if missing:
            result = self._initial_generation(ws, missing)
        else:
            result = self._localized_revision(ws, ctx.extras)
        # 把写作期新收集到的用户问答经单一写入路径持久化到 ws.profile。
        if self._ask_tool is not None and self._ask_tool.collected:
            result.mutations.append(self._ask_tool.persist_mutation())
        if self._gate_violations:
            rejected = list(self._gate_violations)

            def persist_rejections(w: PaperWorkspace) -> None:
                w.artifact_violations.extend(rejected)

            result.mutations.append(persist_rejections)
        return result

    def _make_ask_tool(self, ws: PaperWorkspace) -> AskUserTool | None:
        """仅当注入了交互式 Elicitor 时构造 AskUserTool；否则返回 None（不暴露）。"""
        if not getattr(self._elicitor, "interactive", False):
            return None
        existing = ws.profile.get("clarification_answers") or []
        return AskUserTool(
            self._elicitor, existing_answers=existing, budget=self._ask_user_budget
        )

    def _register_ask_user(self, registry: ToolRegistry) -> None:
        """把 ask_user 工具注册进 registry（仅当本轮启用了 ask 工具）。"""
        if self._ask_tool is not None:
            register_ask_user_tool(registry, self._ask_tool)

    # --- 初次生成 ---

    def _initial_generation(
        self, ws: PaperWorkspace, nodes: list[OutlineNode]
    ) -> AgentResult:
        drafts: dict[str, SectionDraft] = {}
        summaries: dict[str, str] = {}
        logs: list[str] = []

        # 工具模式：整次生成共享一个文献累积器，跨章节复用检索到的文献。
        registry: ToolRegistry | None = None
        lit_tool: LiteratureSearchTool | None = None
        if self._tool_mode:
            registry, lit_tool = build_writing_tools(
                self._retrieval, self._verifier, hooks=self._hooks
            )
            # Round 6：初次生成路径也注册 fetch_paper_section，使写作时可按段落
            # 取已验证文献的 motivation / method / results，而非只看 title。
            from paper_agent.tools.paper_section_tool import (
                register_paper_section_tool,
            )

            register_paper_section_tool(registry, ws)
            # 写作期 ask_user（仅交互模式启用）。
            self._register_ask_user(registry)

        preexisting = set(ws.verified_reference_ids())
        for node in nodes:
            preserved_revision_base = (
                ws.input_mode is InputMode.DRAFT_REVISION
                and bool(ws.draft_sections.get(node.section_id, "").strip())
            )
            artifact_baseline = (
                ws.input_mode is InputMode.GENERATION
                and ws.artifact is not None
                and not ws.artifact.is_empty()
            )
            if preserved_revision_base:
                # Accuracy-first revision starts from the byte-preserved source.
                # Review feedback may request localized edits in later rounds, but
                # the initial pass must never paraphrase away user facts.
                content = ws.draft_sections[node.section_id]
                logs.append(f"保留初稿章节基线：{node.title}")
            elif artifact_baseline:
                # Start from a fact-complete deterministic rendering.  Subsequent
                # guarded review/revision rounds may improve prose, but the first
                # committed draft cannot omit or invent artifact facts.
                content = self._safe_artifact_content(ws, node)
                logs.append(f"生成 Artifact 确定性基线：{node.title}")
            else:
                content = self._write_new(ws, node, registry, lit_tool, logs)
            # 可引用集合 = 已验证库 + 写作中新检索到的（工具模式）。
            available = sorted(
                preexisting | (set(lit_tool.found) if lit_tool else set())
            )
            # 只记录正文实际引用到的文献，避免每章无差别堆砌全部文献。
            cited = self._extract_cited(content, available)
            candidate = self._artifact_checked_draft(
                ws, node, node.title, content, cited, logs
            )
            if candidate is None:
                continue
            drafts[node.section_id] = candidate
            summaries[node.section_id] = (
                content.strip()[:300]
                if preserved_revision_base or artifact_baseline
                else self._ctx.summarize_section(node.title, content)
            )
            logs.append(f"撰写章节：{node.title}")

        figure_captions = self._process_figures(ws)
        if figure_captions:
            logs.append(f"处理图表 {len(figure_captions)} 个")

        # 数据出图（Req 7.3 / 9.1 / 9.4）：启用且有数据时先尝试从实验数据出图，
        # 产出的 FigureRecord 经下方 mutate() 走单一写入路径写回；失败/禁用/无后端/
        # 无数据时 render_data_figures 返回 []，回落既有 LLM 文字图题（_process_figures）。
        data_figures = self._render_data_figures(ws)
        if data_figures:
            logs.append(f"数据出图 {len(data_figures)} 张（rendered_from_data）")

        found_refs = list(lit_tool.found.values()) if lit_tool else []

        def mutate(w: PaperWorkspace) -> None:
            w.section_drafts.update(drafts)
            w.section_summaries.update(summaries)
            existing = {r.id for r in w.verified_references}
            for ref in found_refs:
                if ref.id not in existing:
                    w.verified_references.append(ref)
                    existing.add(ref.id)
            # 先追加数据出图产生的 FigureRecord（单一写入路径，Property 19）；
            # 续跑幂等：figure_id 已存在则跳过，不重复追加（Property 21 / Req 9.4）。
            existing_fig_ids = {f.figure_id for f in w.figures}
            for rec in data_figures:
                if rec.figure_id in existing_fig_ids:
                    continue
                w.figures.append(rec)
                existing_fig_ids.add(rec.figure_id)
            # 再为既有图（用户/占位图）落 LLM 文字图题——数据出图的记录不在此表中，
            # 保留渲染器给出的图题；两类图共存（实验图 + 用户提供图）。
            for fig in w.figures:
                if fig.figure_id in figure_captions:
                    fig.caption = figure_captions[fig.figure_id]

        if found_refs:
            logs.append(f"写作期新检索并核验入库文献 {len(found_refs)} 条")
        return AgentResult(mutations=[mutate], logs=logs)

    def _artifact_checked_draft(
        self,
        ws: PaperWorkspace,
        node: OutlineNode,
        title: str,
        content: str,
        cited: list[str],
        logs: list[str],
    ) -> SectionDraft | None:
        """Construct and validate a candidate before any workspace mutation."""
        contract = ws.artifact.contract() if ws.artifact is not None else None
        evidence_ids = list(
            node.allowed_evidence_ids or node.required_evidence_ids
        )
        candidate = SectionDraft(
            section_id=node.section_id,
            title=title,
            content=content,
            cited_reference_ids=cited,
            artifact_hash=contract.artifact_hash if contract else "",
            evidence_ids=evidence_ids,
            claim_manifest=build_claim_manifest(content, evidence_ids),
        )
        verdict = ArtifactCommitGate().check(ws, node, candidate)
        if verdict.passed:
            return candidate
        self._gate_violations.extend(verdict.high_violations)
        logs.append(
            f"ArtifactCommitGate 拒绝章节《{title}》："
            + "；".join(item["message"] for item in verdict.high_violations[:3])
        )
        # Generation can safely degrade to a deterministic rendering of the
        # artifact.  This keeps unknown model facts out while still producing a
        # complete, auditable section.  Revision never takes this path: preserving
        # the user's source is preferable to silently replacing it.
        if (
            ws.input_mode is InputMode.GENERATION
            and ws.artifact is not None
            and not ws.artifact.is_empty()
        ):
            safe_content = self._safe_artifact_content(ws, node)
            safe_candidate = SectionDraft(
                section_id=node.section_id,
                title=title,
                content=safe_content,
                cited_reference_ids=[],
                artifact_hash=contract.artifact_hash if contract else "",
                evidence_ids=evidence_ids,
                claim_manifest=build_claim_manifest(
                    safe_content, evidence_ids
                ),
            )
            safe_verdict = ArtifactCommitGate().check(
                ws, node, safe_candidate
            )
            if safe_verdict.passed:
                logs.append(f"章节《{title}》已回退为 Artifact 确定性内容")
                return safe_candidate
            self._gate_violations.extend(safe_verdict.high_violations)
        return None

    @staticmethod
    def _safe_artifact_content(
        ws: PaperWorkspace, node: OutlineNode
    ) -> str:
        """Render only user-provided facts; used after a model candidate is rejected."""
        artifact = ws.artifact
        assert artifact is not None
        key = f"{node.section_id} {node.title}".lower()
        lines: list[str] = []
        if any(token in key for token in ("intro", "引言", "绪论")):
            lines.append(artifact.research_question)
            lines.extend(item.summary for item in artifact.contributions)
        elif any(token in key for token in ("experiment", "result", "实验", "结果")):
            for exp in artifact.experiments:
                lines.append(f"### {exp.experiment_id}")
                lines.append(f"数据集：{exp.dataset}")
                lines.append(f"基线：{', '.join(exp.baselines)}")
                lines.append(f"评价指标：{', '.join(exp.metrics)}")
                if exp.hyperparameters:
                    lines.append(
                        "实验设置与超参数："
                        + json.dumps(
                            exp.hyperparameters,
                            ensure_ascii=False,
                            sort_keys=True,
                        )
                    )
                for row in (exp.results_data or {}).get("rows") or []:
                    lines.append(
                        json.dumps(row, ensure_ascii=False, sort_keys=True)
                    )
        elif any(token in key for token in ("conclusion", "结论", "总结")):
            lines.extend(item.summary for item in artifact.contributions)
            lines.extend(artifact.novelty_claims)
        else:
            lines.append(artifact.method.overview)
            lines.extend(artifact.method.key_components)
            if artifact.method.formal_definition:
                lines.append(artifact.method.formal_definition)
            if artifact.method.pseudocode:
                lines.append(artifact.method.pseudocode)
            datasets = [exp.dataset for exp in artifact.experiments if exp.dataset]
            if datasets:
                lines.append("数据集与预处理范围：" + "；".join(datasets))
            settings = {
                exp.experiment_id: exp.hyperparameters
                for exp in artifact.experiments
                if exp.hyperparameters
            }
            if settings:
                lines.append(
                    "实验设置与超参数："
                    + json.dumps(settings, ensure_ascii=False, sort_keys=True)
                )
        return "\n\n".join(line for line in lines if line.strip())

    def _process_figures(self, ws: PaperWorkspace) -> dict[str, str]:
        """为缺少说明的图表生成说明；用户已提供说明的保持不变（Req 6.1/6.2）。"""
        captions: dict[str, str] = {}
        for fig in ws.figures:
            if fig.caption_provided_by_user and fig.caption:
                continue  # Req 6.1：沿用用户提供的说明
            messages = templates.figure_caption(data_ref=fig.data_ref)
            resp = self._llm.complete(messages)
            captions[fig.figure_id] = resp.content.strip()
        return captions

    def _render_data_figures(self, ws: PaperWorkspace) -> list[FigureRecord]:
        """从实验数据出图，返回待经 mutate() 写回的 FigureRecord 列表（Req 7.3 / 9.1 / 9.4）。

        - 启用且有数据 → 调用 FigureRenderer 从 artifact 出图；产出的 FigureRecord
          （rendered_from_data=True）由调用方经单一写入路径追加（Property 19）。
        - 禁用 / 无后端 / 无 artifact / 无数据 / 渲染返回 [] → 返回 []，回落既有
          LLM 文字图题（_process_figures）；渲染器自身优雅降级，绝不中止管线
          （Property 20）。
        - workspace_dir 缺失时优雅跳过数据出图（不报错、不产图）。
        - 续跑幂等（Property 21 / Req 9.4）：跳过 figure_id 已存在于 ws.figures 且
          其资产文件已落盘的图，不重复追加记录、不重复落盘。
        """
        # 缺资产落盘根目录：无法确定资产路径，优雅跳过数据出图（回落文字图题）。
        if self._workspace_dir is None:
            return []

        assets_dir = f"{self._workspace_dir}/{ws.workspace_id}_assets"

        # 优先使用注入的渲染器；否则按需构造默认实例（matplotlib 为可选依赖，
        # 缺失时渲染器自身降级为 []）。grounding 复用质量闸判定逻辑（同源，不放宽）。
        renderer = self._figure_renderer or FigureRenderer(
            backend=MatplotlibBackend(),
            grounding=GroundingChecker(ws.artifact),
            sink=self._sink or NullSink(),
            tracker=None,
            enabled=self._figures_from_data_enabled,
        )

        rendered: list[RenderedFigure] = renderer.render_from_artifact(
            ws.artifact, assets_dir
        )

        existing_fig_ids = {f.figure_id for f in ws.figures}
        new_records: list[FigureRecord] = []
        for fig in rendered:
            record = fig.record
            # 幂等：已有同 figure_id 记录且资产文件已存在 → 跳过，不重复追加。
            if record.figure_id in existing_fig_ids and os.path.exists(fig.asset_path):
                continue
            # 关键修复：导出器按「相对导出目录」解析 data_ref，而导出目录即
            # workspace_dir；图像落在 {workspace_dir}/{ws_id}_assets/ 下，故这里把
            # data_ref 重写为相对 workspace_dir 的路径（正斜杠），否则导出器定位不到
            # 资产、数据图永远走缺资产回退（不嵌入 \includegraphics）。
            try:
                rel = os.path.relpath(fig.asset_path, self._workspace_dir)
                record.data_ref = rel.replace(os.sep, "/")
            except (ValueError, OSError):
                # 跨盘符等异常：保持原 data_ref，最坏退回缺资产回退（不中断）。
                pass
            new_records.append(record)
        return new_records

    def _write_new(
        self,
        ws: PaperWorkspace,
        node: OutlineNode,
        registry: ToolRegistry | None = None,
        lit_tool: LiteratureSearchTool | None = None,
        logs: list | None = None,
    ) -> str:
        from paper_agent.prompts.section_types import infer_and_get_spec  # noqa

        run_context = self._run_context(ws)
        if node.allowed_evidence_ids or node.required_evidence_ids:
            scoped = node.allowed_evidence_ids or node.required_evidence_ids
            run_context += (
                "\n\n[本节事实证据范围]\n"
                + "\n".join(f"- {evidence_id}" for evidence_id in scoped)
                + "\n不得使用范围外的 Artifact 事实；信息不足时省略该细节，"
                "不得输出“待补充”、TODO 或其他占位符。"
            )
        summaries = self._ctx.summaries_block(ws, node.section_id)
        is_revision = (
            ws.input_mode is InputMode.DRAFT_REVISION and bool(ws.original_draft)
        )
        # Round 5：按章节体裁推断差异化写作规约，并注入 prompt。
        section_spec = infer_and_get_spec(node.section_id, node.title)
        # 草稿修订模式：以该章节初稿原文为基底做修订，保留初稿内容与结构，
        # 而非仅凭 300 字片段从零重写（#3 修复）。
        draft_base = ws.draft_sections.get(node.section_id, "") if is_revision else ""
        if is_revision and draft_base:
            messages = templates.revise_section(
                title=node.title,
                suggestion=(
                    "在此前初稿原文基础上润色、补全论据与衔接，保持其核心内容与结构，"
                    "直接输出修改后的完整章节正文。"
                ),
                content=draft_base,
                run_context=run_context,
                section_guidance=section_spec.writing_guidance,
            )
        else:
            messages = templates.writing_section(
                title=node.title,
                hint=node.summary_hint,
                run_context=run_context,
                summaries=summaries,
                is_revision_base=is_revision,
                draft_excerpt=(ws.original_draft or "")[:300] if is_revision else "",
                section_guidance=section_spec.writing_guidance,
            )
        if registry is not None:
            # 工具模式：模型可在写作中按需调用 search_literature。
            # 注入 token 计量器与工具循环配置（Req 7.6/8.1），由其负责历史压缩
            # 与超长结果截断；max_iters 经 ToolLoopConfig 传入，行为与此前一致。
            result = run_tool_loop(
                self._llm,
                messages,
                registry,
                counter=self._counter,
                config=ToolLoopConfig(max_iters=self._max_tool_iters),
            )
            if logs is not None and result.tool_calls_made:
                logs.append(
                    f"《{node.title}》写作中发起 {result.tool_calls_made} 次文献检索"
                )
            return result.content
        return self._llm.complete(messages).content

    def _run_context(self, ws: PaperWorkspace) -> str:
        """运行内稳定的上下文段（大纲 + 术语 + 可引用文献清单 + artifact 摘要），用作缓存前缀。"""
        parts = [
            self._ctx.stable_block(ws),
            f"[可引用的已验证文献]\n{self._citable_refs(ws)}",
            "引用规范：在正文需要支撑处用方括号标注文献 id（例如 [arxiv:1706.03762]）；"
            "只引用确有支撑关系的文献，不要堆砌全部文献。",
        ]
        # Round 7：注入 artifact 摘要——让写作基于用户真实研究而非 LLM 编造。
        artifact_block = self._artifact_block(ws)
        if artifact_block:
            parts.append(artifact_block)
        # 用户澄清偏好（路径 B）：把动态澄清问答的答案作为硬约束注入写作 prompt。
        clarifications = ws.profile.get("clarification_answers") or []
        if clarifications:
            lines = "\n".join(
                f"- 问：{c.get('question', '')}　答：{c.get('answer', '')}"
                for c in clarifications
            )
            parts.append("[用户澄清偏好（须遵循）]\n" + lines)
        return "\n\n".join(parts)

    @staticmethod
    def _artifact_block(ws: PaperWorkspace) -> str:
        """构造 artifact 摘要（仅当 ws.artifact 存在且非空）。

        摘要包含：研究问题、贡献声明（含 evidence_refs）、方法概述、关键组件、
        实验真实数值表。明确指令「正文数字必须能在下表中找到」，让质量闸能据此
        做 grounding 检查。
        """
        artifact = ws.artifact
        if artifact is None or artifact.is_empty():
            return ""

        sections: list[str] = [
            "【用户真实研究内容（正文必须基于此；数字必须来自下表；不得编造）】",
            f"研究问题：{artifact.research_question}",
        ]

        # 方法
        if artifact.method.overview:
            sections.append(f"方法概述：\n{artifact.method.overview}")
        if artifact.method.key_components:
            sections.append(
                "方法关键组件：\n"
                + "\n".join(f"  - {c}" for c in artifact.method.key_components)
            )
        if artifact.method.formal_definition:
            sections.append(f"形式化定义：\n{artifact.method.formal_definition}")
        if artifact.method.pseudocode:
            sections.append(f"伪代码：\n{artifact.method.pseudocode}")

        # 贡献
        if artifact.contributions:
            sections.append("贡献（必须全部兑现）：")
            for i, c in enumerate(artifact.contributions, start=1):
                refs = f"（兑现证据：{', '.join(c.evidence_refs)}）" if c.evidence_refs else ""
                sections.append(f"  {i}. {c.summary} {refs}".strip())

        # 实验 + 数值
        if artifact.experiments:
            sections.append("实验（正文所有数字必须能在以下真实数据中找到）：")
            for exp in artifact.experiments:
                header = (
                    f"  [{exp.experiment_id}] 数据集={exp.dataset}，"
                    f"基线={','.join(exp.baselines)}，"
                    f"指标={','.join(exp.metrics)}"
                )
                sections.append(header)
                if exp.hyperparameters:
                    hp = ", ".join(f"{k}={v}" for k, v in exp.hyperparameters.items())
                    sections.append(f"    超参：{hp}")
                rows = (exp.results_data or {}).get("rows") or []
                if rows:
                    visible_rows = rows[:50]
                    sections.append(
                        "    真实结果行：\n"
                        + "\n".join(
                            "      " + json.dumps(row, ensure_ascii=False, sort_keys=True)
                            for row in visible_rows
                        )
                    )
                    if len(rows) > len(visible_rows):
                        sections.append(
                            f"    （共 {len(rows)} 行；为控制上下文仅展示前 "
                            f"{len(visible_rows)} 行，完整数据仍用于数值闸门。）"
                        )
                stats = (exp.results_data or {}).get("stats") or {}
                if stats:
                    for metric, s in stats.items():
                        if metric in ("n",) or not isinstance(s, dict):
                            continue
                        sections.append(
                            f"    {metric}: "
                            f"mean={s.get('mean'):.3f}, "
                            f"std={s.get('std'):.3f}, "
                            f"min={s.get('min')}, max={s.get('max')}"
                        )

        if artifact.novelty_claims:
            sections.append(
                "新颖性声明（必须经得起对比验证）：\n"
                + "\n".join(f"  - {c}" for c in artifact.novelty_claims)
            )

        if artifact.notes:
            sections.append(f"补充说明：\n{artifact.notes}")

        return "\n".join(sections)

    @staticmethod
    def _extract_cited(content: str, candidate_ids: list[str]) -> list[str]:
        """从正文中提取实际引用到的文献 id。

        仅认显式 ``[id]`` 标注，不做裸子串匹配——避免短 id（如 ``sec_0``、
        ``a``）在正文里大量误命中，也避免章节 id 与文献 id 命名空间混淆。
        """
        return [cid for cid in candidate_ids if f"[{cid}]" in content]

    @staticmethod
    def _citable_refs(ws: PaperWorkspace) -> str:
        """渲染可引用文献清单（仅已验证），供 prompt 注入。"""
        lines = []
        for r in ws.verified_references:
            if r.verified:
                authors = ", ".join(r.authors)
                lines.append(f"- [{r.id}] {authors}. {r.title} ({r.year})")
        return "\n".join(lines) or "（暂无，可不引用）"

    # --- 工具注册（function calling schema 暴露） ---

    def _build_tool_registry(
        self, ws: PaperWorkspace
    ) -> tuple[ToolRegistry, LiteratureSearchTool | None, SectionEditTool]:
        """构造本轮的工具注册表并暴露 function calling schema（Req 6.1）。

        每次运行构造**全新** registry（ToolRegistry.register 对重名会抛错），
        统一注册：search_literature（工具模式下）、read_section、read_reference、
        edit_section、run_quality_gate、check_citations。

        返回 (registry, lit_tool, edit_tool)：
        - lit_tool 为文献检索累积器（非工具模式为 None），其新核验文献由
          WritingAgent 回写工作区；
        - edit_tool 为章节编辑意图累积器，其 ``edits`` 由 WritingAgent 汇聚为
          WorkspaceMutation 落盘（工具本身不直接写工作区，Req 6.9 / 9.1 / 9.3）。
        """
        registry = ToolRegistry(hooks=self._hooks)

        # 1) 写作期按需检索（仅在同时具备检索 + 核验时可用）。
        lit_tool: LiteratureSearchTool | None = None
        if self._tool_mode:
            lit_tool = LiteratureSearchTool(self._retrieval, self._verifier)
            registry.register(
                name="search_literature",
                description="按主题检索并核验真实学术文献，返回可引用的已验证文献清单。"
                "写作中需要引用支撑时调用。",
                handler=lit_tool.search,
                parameters=_SEARCH_SCHEMA,
            )

        # 2) 只读取材（按需读取章节全文 / 文献元数据，不占用上下文）。
        read_tools = WorkspaceReadTools(WorkspaceView(ws))
        registry.register(
            name="read_section",
            description="读取指定 section_id 的章节全文（标题 + 正文 + 引用）。只读，不变更工作区。",
            handler=read_tools.read_section,
            parameters=_READ_SECTION_SCHEMA,
        )
        registry.register(
            name="read_reference",
            description="读取指定 reference_id 的完整文献元数据。只读，不变更工作区。",
            handler=read_tools.read_reference,
            parameters=_READ_REFERENCE_SCHEMA,
        )
        # Round 6：按段落取材——比 read_reference 更聚焦，避免整段 abstract 塞 prompt。
        from paper_agent.tools.paper_section_tool import (
            register_paper_section_tool,
        )

        register_paper_section_tool(registry, ws)

        # 3) 章节级精确编辑（锚点定位；产出 SectionEdit 意图，不直接写工作区）。
        edit_tool = SectionEditTool(ws)
        registry.register(
            name="edit_section",
            description="对章节做锚点定位的精确编辑（替换片段或插入，而非整章重写）。"
            "anchor 必须在目标章节内唯一出现一次，否则返回错误且不产生变更。",
            handler=edit_tool.edit_section,
            parameters=_EDIT_SCHEMA,
        )

        # 4) 只读客观验证（质量闸 + 引用检查）。
        quality_tools = QualityCheckTools(ws, verifier=self._verifier)
        registry.register(
            name="run_quality_gate",
            description="触发确定性质量闸检查，返回问题清单（可为空）。只读，不变更工作区。",
            handler=quality_tools.run_quality_gate,
            parameters=_NO_PARAMS_SCHEMA,
        )
        registry.register(
            name="check_citations",
            description="校验正文引用的文献 id 是否都在已验证库中，返回未通过的 id 清单。只读。",
            handler=quality_tools.check_citations,
            parameters=_NO_PARAMS_SCHEMA,
        )

        # 5) 写作期 ask_user（仅交互模式启用；非交互不注册、零影响）。
        self._register_ask_user(registry)

        return registry, lit_tool, edit_tool

    # --- 局部修改 ---

    def _localized_revision(self, ws: PaperWorkspace, extras: dict) -> AgentResult:
        edits: dict[str, str] = extras.get("edits", {})
        gate_fixes: dict[str, list[str]] = extras.get("gate_fixes", {})
        structural = extras.get("structural")

        # #11：合并硬修复（gate 高严重度）与软建议（评审反馈），按来源标注，
        # 让模型能区分「必须修复的客观问题」与「可酌情采纳的主观建议」。
        # 目标章节 = 两类来源的并集；均无则本轮不改动（#4：不再无意义重写）。
        combined: dict[str, str] = {}
        for sid in set(edits) | set(gate_fixes):
            parts: list[str] = []
            fixes = gate_fixes.get(sid)
            if fixes:
                parts.append("【必须修复·客观】" + "；".join(fixes))
            sug = edits.get(sid)
            if sug:
                parts.append("【评审建议·主观】" + sug)
            combined[sid] = "\n".join(parts)

        if not combined and not structural:
            return AgentResult(
                mutations=[],
                logs=["无可执行修订目标，本轮不改动任何章节。"],
            )

        # 工具模式：模型在工具循环中用 read_section/edit_section 等做精确编辑，
        # WritingAgent 汇聚累积的 SectionEdit 为 WorkspaceMutation 落盘。
        #
        # 修订路由（Req 1.1/1.7/3.1/3.2）：对每个目标章节判定走 Patch_Mode
        # （补丁优先）还是 Whole_Section_Regeneration（整章重写）。当前落地判据：
        # 结构型改动指向本章节 → WHOLE_SECTION；否则默认 PATCH_MODE（当
        # patch_first_enabled 开启）。运行期回退判据（全锚点失败 / 超
        # patch_size_limit）在后续任务落地。Patch_Mode 依赖工具模式提供的
        # SectionEdit 累积流程；非工具模式下即便路由为 PATCH_MODE 也只能以
        # 整章重写实现，故此处一并归入 whole 分支。
        #
        # 三态终止保证（Req 3.5）：本方法对任意输入恰产出一个 AgentResult，
        # 其 mutate 闭包确定性地落到三态之一——{已应用补丁 / 完成整章重写 /
        # 无可应用变更且不改动工作区}，不引入任何循环，故必然有限步终止。
        routes = {
            sid: self._route_revision(ws, sid, suggestion, structural)
            for sid, suggestion in combined.items()
        }
        # 记录本轮各目标章节的修订路径（Req 4.3），并对「因结构型改动而走整章
        # 重写」的章节标识回退原因（Req 4.2）。路径取值映射到需求枚举展示名
        # {Patch_Mode, Whole_Section_Regeneration}；非工具模式下即便路由为
        # PATCH_MODE 也只能以整章重写实现，故据实际执行路径归并上报。
        for sid, route in routes.items():
            effective = (
                RevisionRoute.PATCH_MODE
                if route is RevisionRoute.PATCH_MODE and self._tool_mode
                else RevisionRoute.WHOLE_SECTION
            )
            route_label = (
                "Patch_Mode"
                if effective is RevisionRoute.PATCH_MODE
                else "Whole_Section_Regeneration"
            )
            self._emit_revision_event(
                f"修订路径：章节 {sid} → {route_label}",
                {
                    "event": "revision_route",
                    "section_id": sid,
                    "route": effective.value,
                    "route_label": route_label,
                },
            )
            # 结构型改动指向本章节 → 记录回退原因（Req 4.2）。
            if self._structural_targets(structural, sid):
                self._emit_revision_event(
                    f"回退整章重写：章节 {sid}（{FallbackReason.STRUCTURAL_CHANGE.value}）",
                    {
                        "event": "revision_fallback",
                        "section_id": sid,
                        "fallback_reason": FallbackReason.STRUCTURAL_CHANGE.value,
                    },
                )
        patch_edits = {
            sid: suggestion
            for sid, suggestion in combined.items()
            if routes[sid] is RevisionRoute.PATCH_MODE and self._tool_mode
        }
        whole_edits = {
            sid: suggestion
            for sid, suggestion in combined.items()
            if sid not in patch_edits
        }

        # 同一轮同时出现两种路由：分别驱动两条既有流程，合并各自 mutation。
        # 两组目标章节互斥，mutation 依次施加到同一工作区互不干扰；结构型改动
        # 仅由整章重写分支施加一次，避免重复应用。
        if patch_edits and whole_edits:
            tools_result = self._localized_revision_with_tools(ws, patch_edits, None)
            llm_result = self._localized_revision_llm(ws, whole_edits, structural)
            return AgentResult(
                mutations=[*tools_result.mutations, *llm_result.mutations],
                logs=[*tools_result.logs, *llm_result.logs],
            )
        if patch_edits:
            return self._localized_revision_with_tools(ws, patch_edits, structural)

        return self._localized_revision_llm(ws, whole_edits, structural)

    def _route_revision(
        self,
        ws: PaperWorkspace,
        section_id: str,
        suggestion: str,
        structural: dict | None,
    ) -> RevisionRoute:
        """判定某章节本轮修订走 Patch_Mode 还是 Whole_Section_Regeneration。

        判据（Req 1.1 / 1.7 / 3.2；运行期回退判据见后续任务）：
        - `structural` 指向本章节（新增/删除）→ `WHOLE_SECTION`（结构型，Req 3.2）；
        - 否则默认 `PATCH_MODE`（内容型且 `patch_first_enabled` 开启，Req 1.1/1.7）；
        - `patch_first_enabled` 关闭时退化为 `WHOLE_SECTION`（向后兼容整章重写）。

        运行期回退（全部补丁锚点失败 / 补丁累计影响占比 > `patch_size_limit`）在
        补丁物化阶段（任务 8.1）落地，此处仅确定初始路由。
        """
        if self._structural_targets(structural, section_id):
            return RevisionRoute.WHOLE_SECTION
        if self._patch_first_enabled:
            return RevisionRoute.PATCH_MODE
        return RevisionRoute.WHOLE_SECTION

    @staticmethod
    def _structural_targets(structural: dict | None, section_id: str) -> bool:
        """判断结构型改动是否指向给定章节（新增或删除其中之一）。"""
        if not structural:
            return False
        if section_id in structural.get("remove", []):
            return True
        for node in structural.get("add", []):
            sid = getattr(node, "section_id", None)
            if sid is None and isinstance(node, dict):
                sid = node.get("section_id")
            if sid == section_id:
                return True
        return False

    # --- 修订差量可观测（Req 4.1–4.5） ---

    def _emit_revision_event(self, message: str, data: dict) -> None:
        """经既有 EventSink 发出结构化修订事件（复用 EventKind.AGENT_LOG）。

        - `self._sink is None` 时静默跳过（无可观测开销，保持向后兼容）；既有
          `AgentResult.logs` 仍照常返回，事件为附加通道而非替代（不改变返回契约）。
        - 脱敏保证（Req 4.4）：载荷仅承载结构化字段（section_id / 路径 / 计数 /
          回退原因枚举值等），绝不写入 API 密钥或完整请求体；`message` 与任何文本
          型字段一律截断至 `_OBS_EXCERPT_MAX`（2000）字符。
        - Token 用量（Req 4.5）：本轮 LLM 调用的 token 用量已由 provider 层的可观测
          LLM 包装器经既有 UsageTracker 记录，故此处无需（也不应）另造 tracker；
          发出「本轮修订路径」事件即足以支撑 Patch_Mode 与 Whole_Section_Regeneration
          的用量对比（把用量按路径归并）。
        """
        if self._sink is None:
            return
        safe = dict(data)
        for key, value in list(safe.items()):
            if isinstance(value, str) and len(value) > _OBS_EXCERPT_MAX:
                safe[key] = value[:_OBS_EXCERPT_MAX]
        self._sink.emit(
            Event(
                kind=EventKind.AGENT_LOG,
                message=message[:_OBS_EXCERPT_MAX],
                data=safe,
            )
        )

    def _localized_revision_with_tools(
        self,
        ws: PaperWorkspace,
        edits: dict[str, str],
        structural: dict | None,
    ) -> AgentResult:
        """工具循环驱动的局部修订：汇聚 SectionEdit → WorkspaceMutation（Req 6 / 9）。"""
        logs: list[str] = []
        registry, lit_tool, edit_tool = self._build_tool_registry(ws)

        # 逐个目标章节驱动一次有界工具循环；模型可读取/精确编辑/做客观验证。
        from paper_agent.prompts.section_types import infer_and_get_spec  # noqa

        for sid, suggestion in edits.items():
            existing = ws.section_drafts.get(sid)
            if existing is None:
                continue
            section_spec = infer_and_get_spec(existing.section_id, existing.title)
            messages = templates.revise_section(
                title=existing.title,
                suggestion=suggestion,
                content=existing.content,
                run_context=self._run_context(ws),
                section_guidance=section_spec.writing_guidance,
            )
            result = run_tool_loop(
                self._llm,
                messages,
                registry,
                counter=self._counter,
                config=ToolLoopConfig(max_iters=self._max_tool_iters),
            )
            if result.tool_calls_made:
                logs.append(
                    f"《{existing.title}》修订中发起 {result.tool_calls_made} 次工具调用"
                )

        # 汇聚本轮累积的 SectionEdit：仅对目标 section_id 应用（锚点替换/插入），
        # 未涉及章节字节级不变（Property 9）。
        updated, updated_summaries, apply_logs = self._materialize_edits(
            ws, edit_tool.edits
        )
        logs.extend(apply_logs)

        found_refs = list(lit_tool.found.values()) if lit_tool else []

        def mutate(w: PaperWorkspace) -> None:
            # 经仓储原子落盘的唯一写入路径（Req 9.1 / 9.3）。
            w.section_drafts.update(updated)
            w.section_summaries.update(updated_summaries)
            existing_ids = {r.id for r in w.verified_references}
            for ref in found_refs:
                if ref.id not in existing_ids:
                    w.verified_references.append(ref)
                    existing_ids.add(ref.id)
            if structural:
                self._apply_structural(w, structural)

        if found_refs:
            logs.append(f"修订期新检索并核验入库文献 {len(found_refs)} 条")
        if not updated and not structural:
            logs.append("本轮工具循环未产生可应用的精确编辑")
        return AgentResult(mutations=[mutate], logs=logs)

    def _materialize_edits(
        self, ws: PaperWorkspace, section_edits: list[SectionEdit]
    ) -> tuple[dict[str, SectionDraft], dict[str, str], list[str]]:
        """把累积的 SectionEdit 物化为目标章节的新内容（仅目标章节）。

        对每个目标章节，从其当前内容出发按序应用各条编辑；逐条在「应用时刻的
        当前内容」上重新定位锚点，遵循以下门控（Req 1.4/1.5/2.3/2.4/2.5/3.2）：

        - **锚点唯一性门控（Req 1.4/2.4）**：仅当锚点在当前内容中命中次数 == 1
          时方可应用；命中 0 或 >1 → 跳过并记录含**实际命中次数**的原因（Req 1.5/2.4）。
        - **改动区间重叠检测（Req 2.5）**：维护「已成功应用补丁改动的字符区间」
          集合（半开区间，随每次成功应用整体重定位到当前内容坐标）；若某条补丁的
          锚点区间与任一已应用区间重叠 → 跳过、当前内容字节级不变，并记录
          「锚点区间冲突已跳过」。
        - **补丁累计影响上限（Req 3.2）**：累计跟踪本章节成功补丁带来的字符数
          净影响；一旦其占「当前内容字符数」的比例超过 `self._patch_size_limit`，
          即判定应整章重写——**丢弃本章节本轮全部补丁结果**（当前内容回到章节原文）、
          将该 `section_id` 记入 `self._patch_size_exceeded_sections` 以暴露决策，
          并记录含「超过补丁适用上限」的回退原因（实际回退由既有修订路由承接）。

        未触及文本字节级保留、仅目标章节被更新（Property 1）；返回契约保持不变。
        """
        updated: dict[str, SectionDraft] = {}
        updated_summaries: dict[str, str] = {}
        logs: list[str] = []
        # 每次调用重置暴露集合，避免跨轮残留（非破坏式决策暴露，Req 3.2）。
        self._patch_size_exceeded_sections = set()

        by_section: dict[str, list[SectionEdit]] = {}
        for edit in section_edits:
            by_section.setdefault(edit.section_id, []).append(edit)

        for sid, sec_edits in by_section.items():
            existing = ws.section_drafts.get(sid)
            if existing is None:
                # 防御式：累积期已校验存在性，此处仍跳过未知章节（Req 9.4）。
                continue
            content = existing.content
            applied = 0
            # 被跳过补丁计数与「因锚点未唯一命中而跳过」计数（供 Req 4.1 载荷与
            # Req 4.2 回退原因判定）。
            skipped = 0
            anchor_miss = 0
            # 已成功补丁改动的字符区间（半开 [start, end)，当前内容坐标系）。
            changed_intervals: list[tuple[int, int]] = []
            # 补丁累计字符数净影响；比例阈值以「当前内容长度」为分母（Req 3.2）。
            cumulative_impact = 0
            exceeded = False

            for edit in sec_edits:
                anchor = edit.anchor
                # 空锚点无法定位，跳过（既有 _apply_section_edit 亦拒绝）。
                if not anchor:
                    skipped += 1
                    logs.append(
                        f"章节《{existing.title}》一处精确编辑的锚点为空，已跳过"
                    )
                    continue
                # 逐条在当前内容重新定位；仅唯一命中方可应用（Req 1.4/2.4）。
                hits = content.count(anchor)
                if hits != 1:
                    skipped += 1
                    anchor_miss += 1
                    logs.append(
                        f"章节《{existing.title}》一处精确编辑锚点未唯一命中"
                        f"（实际命中 {hits} 次），已跳过"
                    )
                    continue
                idx = content.index(anchor)
                end = idx + len(anchor)
                # 改动区间重叠检测（Req 2.5）：锚点区间 [idx, end) 与任一已应用
                # 改动区间重叠则跳过；半开区间重叠判据 a < end 且 idx < b。
                if any(a < end and idx < b for (a, b) in changed_intervals):
                    skipped += 1
                    logs.append(
                        f"章节《{existing.title}》一处精确编辑：锚点区间冲突已跳过"
                    )
                    continue

                new_content, ok = self._apply_section_edit(content, edit)
                if not ok:
                    # 走到此处仅剩「mode 非法」一种可能（唯一命中已确认）。
                    skipped += 1
                    logs.append(
                        f"章节《{existing.title}》一处精确编辑的 mode 取值非法，已跳过"
                    )
                    continue

                # 计算本次改动区间（新内容坐标）与用于重定位既有区间的位移点。
                repl_len = len(edit.replacement)
                if edit.mode == "replace":
                    delta = repl_len - (end - idx)
                    new_interval = (idx, idx + repl_len)
                    shift_point = end
                elif edit.mode == "insert_after":
                    delta = repl_len
                    new_interval = (end, end + repl_len)
                    shift_point = end
                else:  # insert_before
                    delta = repl_len
                    new_interval = (idx, idx + repl_len)
                    shift_point = idx

                # 将既有改动区间整体重定位到新内容坐标：位于位移点及其后的区间
                # 随 delta 平移；位移点之前的区间不变（无重叠保证其不跨越位移点）。
                relocated: list[tuple[int, int]] = []
                for a, b in changed_intervals:
                    if a >= shift_point:
                        relocated.append((a + delta, b + delta))
                    else:
                        relocated.append((a, b))
                # 仅登记非空改动区间；空插入（replacement 为空）不改字节、不占区间。
                if new_interval[1] > new_interval[0]:
                    relocated.append(new_interval)
                changed_intervals = relocated

                content = new_content
                applied += 1
                # 净影响以字符数绝对变化计量，分母取当前内容长度（Req 3.2）。
                cumulative_impact += abs(delta)
                limit_chars = self._patch_size_limit * len(content)
                if cumulative_impact > limit_chars:
                    exceeded = True
                    break

            if exceeded:
                # 超过补丁适用上限：丢弃本章节本轮全部补丁、内容回到原文（字节级
                # 不变），记录回退原因并暴露该章节供整章重写路由承接（Req 3.2）。
                self._patch_size_exceeded_sections.add(sid)
                logs.append(
                    f"章节《{existing.title}》补丁累计影响 {cumulative_impact} 字符"
                    f"（当前内容 {len(content)} 字符，上限比例 "
                    f"{self._patch_size_limit}）——超过补丁适用上限，"
                    f"丢弃本轮补丁并回退整章重写"
                )
                # 回退可观测：标识回退原因「超过补丁适用上限」（Req 4.2）。
                self._emit_revision_event(
                    f"回退整章重写：章节 {sid}（{FallbackReason.PATCH_SIZE_EXCEEDED.value}）",
                    {
                        "event": "revision_fallback",
                        "section_id": sid,
                        "fallback_reason": FallbackReason.PATCH_SIZE_EXCEEDED.value,
                        "applied": applied,
                        "skipped": skipped,
                    },
                )
                continue

            if applied == 0 or content == existing.content:
                # 本章节无任何补丁成功应用：若存在补丁且全部因锚点未唯一命中被跳过
                # （applied==0 且 skipped 全部为锚点未命中），标识回退原因「锚点未唯一
                # 命中」（Req 3.1 / 4.2）；实际整章重写由既有修订路由承接。
                if applied == 0 and skipped > 0 and skipped == anchor_miss:
                    self._emit_revision_event(
                        f"回退整章重写：章节 {sid}（{FallbackReason.ANCHOR_NOT_UNIQUE.value}）",
                        {
                            "event": "revision_fallback",
                            "section_id": sid,
                            "fallback_reason": FallbackReason.ANCHOR_NOT_UNIQUE.value,
                            "applied": applied,
                            "skipped": skipped,
                        },
                    )
                continue
            # 补丁模式成功应用 ≥1 条：发出 diff 决策事件，载荷含 section_id、成功
            # 应用数与被跳过数（Req 4.1）；载荷仅结构化字段，无密钥/请求体（Req 4.4）。
            self._emit_revision_event(
                f"补丁应用：章节 {sid}（应用 {applied} 处，跳过 {skipped} 处）",
                {
                    "event": "patch_applied",
                    "section_id": sid,
                    "route": RevisionRoute.PATCH_MODE.value,
                    "applied": applied,
                    "skipped": skipped,
                },
            )
            # 修订后从新正文重新提取引用，避免记录字段与正文不同步
            # （Property 1：正文 [id] 必须来自已验证库，由质量闸正文扫描兜底）。
            cited = self._extract_cited(content, sorted(ws.verified_reference_ids()))
            node = next(
                (item for item in ws.outline if item.section_id == sid),
                OutlineNode(
                    section_id=sid,
                    title=existing.title,
                    order=0,
                    required_evidence_ids=list(existing.evidence_ids),
                    allowed_evidence_ids=list(existing.evidence_ids),
                ),
            )
            candidate = self._artifact_checked_draft(
                ws, node, existing.title, content, cited, logs
            )
            if candidate is None:
                continue
            updated[sid] = candidate
            updated_summaries[sid] = self._ctx.summarize_section(
                existing.title, content
            )
            logs.append(f"局部修订章节：{existing.title}（应用 {applied} 处精确编辑）")

        return updated, updated_summaries, logs

    @staticmethod
    def _apply_section_edit(content: str, edit: SectionEdit) -> tuple[str, bool]:
        """按 anchor + mode 应用单条 SectionEdit。返回 (新内容, 是否已应用)。

        仅当锚点在当前内容中唯一命中（命中次数 == 1）时应用，否则不变更并返回
        (原内容, False)，保证局部编辑不外溢（Property 9）。
        """
        anchor = edit.anchor
        if not anchor:
            return content, False
        if content.count(anchor) != 1:
            return content, False

        idx = content.index(anchor)
        end = idx + len(anchor)
        if edit.mode == "replace":
            return content[:idx] + edit.replacement + content[end:], True
        if edit.mode == "insert_after":
            return content[:end] + edit.replacement + content[end:], True
        if edit.mode == "insert_before":
            return content[:idx] + edit.replacement + content[idx:], True
        return content, False

    def _localized_revision_llm(
        self,
        ws: PaperWorkspace,
        edits: dict[str, str],
        structural: dict | None,
    ) -> AgentResult:
        """整章重写式局部修订（无工具模式的既有路径，保持向后兼容）。

        对每个目标章节调用 ``_revise_content`` 产出新内容，并经 Content_Contract
        校验后决定是否采纳（Req 3.3 / 3.4 / 3.6 / 3.7）：

        - LLM 调用失败 / 抛错 / 超时 → 不改动工作区、记录失败原因（Req 3.7）；
        - 产物含 ``unknown_construct`` 违规（契约子集之外的构造）→ 丢弃产物、
          目标章节内容字节级不变、记录可诊断原因（Req 3.6）；
        - 否则采纳归一化后的内容（Req 3.4，产物符合 Content_Contract）；
          ``unknown_citation`` / ``unknown_figure`` / ``length_exceeded`` 为诊断项，
          不触发丢弃（内容保留）。

        所有写回仅经 ``AgentResult.mutations`` → ``WorkspaceRepository`` 单一原子
        写入路径（Req 3.3）。
        """
        logs: list[str] = []

        # 计算需要更新的章节内容（仅目标章节，其余保持不变）。
        updated: dict[str, SectionDraft] = {}
        updated_summaries: dict[str, str] = {}

        for sid, suggestion in edits.items():
            existing = ws.section_drafts.get(sid)
            if existing is None:
                continue

            # 整章重写产出新内容；LLM 调用失败 / 抛错 / 超时 → 不改动工作区，
            # 记录失败原因（Req 3.7）。异常被局部捕获，逐章隔离——单章失败不
            # 影响本轮其余章节的修订。
            try:
                raw_content = self._revise_content(ws, existing, suggestion)
            except Exception as exc:  # noqa: BLE001 —— 外部 LLM 输出视为不可信
                logs.append(
                    f"章节《{existing.title}》整章重写调用失败"
                    f"（{type(exc).__name__}: {exc}）——不改动工作区（Req 3.7）"
                )
                continue

            # 契约校验（Req 3.4 / 3.6）：先 normalize 再 validate。仅
            # ``unknown_construct``（契约子集之外的构造）判定为不合规 →
            # 丢弃产物、目标章节内容字节级不变、记录可诊断原因（Req 3.6）。
            # ``unknown_citation`` / ``unknown_figure`` / ``length_exceeded``
            # 为诊断项，不触发丢弃（内容保留），仅记录以便可观测。
            normalized = content_contract.normalize(raw_content)
            violations = content_contract.validate(normalized.content, ws)
            unknown_constructs = [
                v for v in violations if v.kind == "unknown_construct"
            ]
            if unknown_constructs:
                first = unknown_constructs[0]
                logs.append(
                    f"章节《{existing.title}》整章重写产物含契约外构造"
                    f"（{len(unknown_constructs)} 处，首处 line={first.line} "
                    f"column={first.column}：{first.message}）——判定不合规，"
                    f"丢弃产物并保留原章节内容字节级不变（Req 3.6）"
                )
                continue

            # 记录非致命诊断（不丢弃、内容保留）。
            for v in violations:
                logs.append(
                    f"章节《{existing.title}》整章重写产物诊断（{v.kind}）："
                    f"{v.message}"
                )

            # 产物符合 Content_Contract（Req 3.4）：接受归一化后的内容。
            new_content = normalized.content
            # 修订后从新正文重新提取引用，避免记录字段与正文不同步（Property 1）。
            cited = self._extract_cited(new_content, sorted(ws.verified_reference_ids()))
            node = next(
                (item for item in ws.outline if item.section_id == sid),
                OutlineNode(
                    section_id=sid,
                    title=existing.title,
                    order=0,
                    required_evidence_ids=list(existing.evidence_ids),
                    allowed_evidence_ids=list(existing.evidence_ids),
                ),
            )
            candidate = self._artifact_checked_draft(
                ws, node, existing.title, new_content, cited, logs
            )
            if candidate is None:
                continue
            updated[sid] = candidate
            updated_summaries[sid] = self._ctx.summarize_section(
                existing.title, new_content
            )
            logs.append(f"局部修订章节：{existing.title}")

        def mutate(w: PaperWorkspace) -> None:
            # 仅更新目标章节，未涉及章节字节级保持不变（Property 5）。
            w.section_drafts.update(updated)
            w.section_summaries.update(updated_summaries)
            if structural:
                self._apply_structural(w, structural)

        if not edits and not structural:
            logs.append("无修订建议，本轮不改动")
        return AgentResult(mutations=[mutate], logs=logs)

    def _revise_content(
        self, ws: PaperWorkspace, existing: SectionDraft, suggestion: str
    ) -> str:
        from paper_agent.prompts.section_types import infer_and_get_spec  # noqa

        section_spec = infer_and_get_spec(existing.section_id, existing.title)
        messages = templates.revise_section(
            title=existing.title,
            suggestion=suggestion,
            content=existing.content,
            run_context=self._run_context(ws),
            section_guidance=section_spec.writing_guidance,
        )
        return self._llm.complete(messages).content

    @staticmethod
    def _apply_structural(w: PaperWorkspace, structural: dict) -> None:
        """结构型修订：在受影响范围内新增/删除章节（Req 5.9）。"""
        for node in structural.get("add", []):
            w.outline.append(node)
        for sid in structural.get("remove", []):
            w.outline = [n for n in w.outline if n.section_id != sid]
            w.section_drafts.pop(sid, None)
            w.section_summaries.pop(sid, None)
