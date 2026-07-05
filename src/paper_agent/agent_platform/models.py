"""智能体平台数据模型。

集中定义平台层的纯数据结构（dataclass），零业务逻辑、零外部依赖，便于序列化
与测试。分四组：

1. 任务与会话：``WritingTask`` / ``AgentSession`` / ``TaskResult``。
2. 循环配置：``TaskAgentConfig``（带取值范围，越界校验交由装配层）。
3. 护栏产物：``RejectedChange`` / ``GateOutcome``。
4. 工具与排版：``ToolSpec`` / ``Typesetting``。

约定：可持久化的结构提供 ``to_dict`` / ``from_dict``；含运行期不可序列化字段
（如 ``WorkspaceMutation`` 可调用对象、``PaperWorkspace`` 引用）的结构不做整体
序列化——其持久化经既有 ``PaperWorkspace`` 通道完成。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # 仅类型标注需要，避免运行期循环导入。
    from paper_agent.agents.base import WorkspaceMutation
    from paper_agent.workspace.models import PaperWorkspace, ReferenceEntry
    from paper_agent.workspace.research_artifact import ResearchArtifact


# --------------------------------------------------------------------------- #
# 1. 任务与会话
# --------------------------------------------------------------------------- #

@dataclass
class WritingTask:
    """用户下达的一个自然语言学术写作任务及其可选上下文（Req 1）。

    ``instruction`` 为自由文本任务描述，是唯一必填项；其余字段提供任务上下文：
    - ``workspace_id``：已有工作区 id（局部修改 / 续跑场景）。
    - ``draft_path``：初稿文件路径（首次处理一篇稿件）。
    - ``topic_background``：主题背景（从零生成场景）。
    - ``artifact``：结构化真实研究内容（反幻觉 grounding）。
    - ``profile``：论文档案 / steering 偏好（目标期刊、风格等）。

    平台不要求用户预先把任务归类到任何固定处理模式（Req 1.5）。
    """

    instruction: str
    workspace_id: str | None = None
    draft_path: str | None = None
    topic_background: str | None = None
    artifact: "ResearchArtifact | None" = None
    profile: dict = field(default_factory=dict)

    def has_instruction(self) -> bool:
        """``instruction`` 是否为「已提供」（非空且非纯空白）（Req 1.4）。"""
        return bool(self.instruction and self.instruction.strip())


@dataclass
class AgentSession:
    """一次任务的执行会话（Req 9）。

    ``session_id`` 复用工作区 id，使续跑天然依托既有工作区持久化（设计取舍）。
    ``transcript`` 记录工具调用与关键决策，供可观测与可复现；其内容为纯 dict，
    随 ``PaperWorkspace.profile`` 一并持久化（``WorkspaceMutation`` 等运行期对象
    不入 transcript）。
    """

    session_id: str
    workspace: "PaperWorkspace"
    task: WritingTask
    transcript: list[dict] = field(default_factory=list)

    def record(self, kind: str, **fields_: Any) -> None:
        """向 transcript 追加一条可观测记录（防御式：值一律可序列化）。"""
        entry = {"kind": str(kind)}
        for key, value in fields_.items():
            entry[key] = value if _is_jsonish(value) else str(value)
        self.transcript.append(entry)


@dataclass
class TaskResult:
    """一次任务的最终结果，面向用户诚实反馈（Req 8 / 9）。

    - ``summary``：结果与实际采取的关键处理决策（Req 8.5）。
    - ``completed`` / ``unfinished``：已完成 / 未完成部分（含原因）（Req 8.2/8.3）。
    - ``guardrail_report``：护栏各维度通过 / 未通过情况（Req 5.4）。
    - ``bound_hit``：触达的有界性上限类型（``None`` 表示正常收尾）（Req 9.2）。
    - ``export_files``：产出文件路径列表。
    """

    session_id: str
    summary: str = ""
    completed: list[str] = field(default_factory=list)
    unfinished: list[str] = field(default_factory=list)
    guardrail_report: dict = field(default_factory=dict)
    bound_hit: str | None = None
    export_files: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "session_id": self.session_id,
            "summary": self.summary,
            "completed": list(self.completed),
            "unfinished": list(self.unfinished),
            "guardrail_report": dict(self.guardrail_report),
            "bound_hit": self.bound_hit,
            "export_files": list(self.export_files),
        }


# --------------------------------------------------------------------------- #
# 2. 循环配置
# --------------------------------------------------------------------------- #

@dataclass
class TaskAgentConfig:
    """顶层工具循环的可调参数（Req 9.1）。

    取值范围（越界校验由装配层负责，见 ``app.build_task_agent``）：
    - ``max_iters``：1..100，顶层任务通常比子循环需要更多轮。
    - ``context_token_budget``：1..1_000_000，触发历史压缩的累计 token 阈值。
    - ``max_tool_result_tokens``：100..100_000，单个工具结果截断上限。
    - ``keep_recent_turns``：1..50，压缩时保留最近 N 轮原文。
    """

    max_iters: int = 12
    context_token_budget: int = 32_000
    max_tool_result_tokens: int = 2_000
    keep_recent_turns: int = 3


# --------------------------------------------------------------------------- #
# 3. 护栏产物
# --------------------------------------------------------------------------- #

# 改动类别：内容改动（走质量/忠实性护栏）与引用增补（走引用真实性护栏）。
CHANGE_CONTENT = "content"
CHANGE_CITATION = "citation"


@dataclass
class ProposedChange:
    """一个「带元数据的更新意图」——改工作区工具的统一产物（设计中的 Mutation_Intent）。

    相比裸 ``WorkspaceMutation`` 可调用对象，本载体额外携带护栏审查所需的元数据，
    使闸门能**逐条**判定通过/拒绝，而非黑盒：
    - ``mutation``：真正对工作区生效的更新函数（不可序列化，运行期使用）。
    - ``kind``：``CHANGE_CONTENT`` 或 ``CHANGE_CITATION``，决定走哪条护栏。
    - ``section_id``：内容改动的目标章节（用于按章节归因质量/忠实性问题）。
    - ``references``：引用增补时的候选文献（逐条核验可核验性）。
    - ``describe``：人类可读描述（回灌 / 上报用）。
    """

    mutation: "WorkspaceMutation"
    kind: str = CHANGE_CONTENT
    section_id: str = ""
    references: list["ReferenceEntry"] = field(default_factory=list)
    describe: str = ""


@dataclass
class RejectedChange:
    """一条未通过护栏闸门、被拒绝落盘的改动（Req 5.3）。

    ``reason`` 为人类可读原因（回灌给智能体供修正）；``dimension`` 标识失败的护栏
    维度（如 ``"faithfulness"`` / ``"quality"`` / ``"citation"``），可为空。
    """

    section_id: str
    reason: str
    dimension: str = ""


@dataclass
class GateOutcome:
    """护栏闸门 ``screen`` 的产物（Req 4 / 5）。

    - ``accepted_mutations``：通过校验、可交由单一写路径落盘的更新意图。
    - ``rejected``：未通过的改动及其原因。
    - ``notes``：差额 / 降级等人类可读说明（回灌智能体并上报用户）。

    不变式：一批被审查的改动，最终要么进入 ``accepted_mutations``、要么进入
    ``rejected``，二者划分完备且互不重叠（见护栏闸门单元测试）。
    """

    passed: bool
    accepted_mutations: list["WorkspaceMutation"] = field(default_factory=list)
    rejected: list[RejectedChange] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


# --------------------------------------------------------------------------- #
# 4. 工具与排版
# --------------------------------------------------------------------------- #

@dataclass
class ToolSpec:
    """外部工具（MCP / skills）被发现时的描述（Req 7.3）。

    以与内建工具一致的形态（名称 / 描述 / 参数 JSON Schema）暴露给注册表，
    使 Agent_Loop 无需区分内建与外部工具。
    """

    name: str
    description: str
    parameters_schema: dict = field(
        default_factory=lambda: {"type": "object", "properties": {}}
    )


# DOCX 段落对齐方式的合法取值（与 python-docx WD_ALIGN_PARAGRAPH 概念对应）。
ALIGNMENT_VALUES = ("left", "center", "right", "justify")


@dataclass
class Typesetting:
    """DOCX 正文段落排版规格（Req 6 场景）。

    各字段默认 ``None``，语义为「未指定」——未指定的字段在导出时沿用既有默认
    行为，不强加样式。字段：
    - ``line_spacing``：行距（磅值或倍数，交由导出器解释）。
    - ``alignment``：对齐方式，取值见 ``ALIGNMENT_VALUES``。
    - ``first_line_indent``：首行缩进（如 ``"2ch"`` / 磅值字符串，交由导出器解释）。
    - ``font``：正文字体名。
    - ``columns``：分栏数（节级排版原语；如 2 表示双栏，小论文常用）。1/None 表示单栏/未指定。
    """

    line_spacing: float | None = None
    alignment: str | None = None
    first_line_indent: str | None = None
    font: str | None = None
    columns: int | None = None

    def is_empty(self) -> bool:
        """是否所有字段都「未指定」（此时导出器完全走默认行为）。"""
        return all(
            v is None
            for v in (
                self.line_spacing, self.alignment, self.first_line_indent,
                self.font, self.columns,
            )
        )

    def to_dict(self) -> dict:
        """仅序列化「已指定」字段，便于紧凑存入 ``ws.profile``。"""
        data: dict = {}
        if self.line_spacing is not None:
            data["line_spacing"] = self.line_spacing
        if self.alignment is not None:
            data["alignment"] = self.alignment
        if self.first_line_indent is not None:
            data["first_line_indent"] = self.first_line_indent
        if self.font is not None:
            data["font"] = self.font
        if self.columns is not None:
            data["columns"] = self.columns
        return data

    @classmethod
    def from_dict(cls, data: dict | None) -> "Typesetting":
        """从 dict 还原；缺失键即「未指定」。``None``/空 dict → 全未指定。"""
        data = data or {}
        alignment = data.get("alignment")
        if alignment is not None and alignment not in ALIGNMENT_VALUES:
            # 防御式：非法对齐值视为未指定，不因脏数据破坏导出。
            alignment = None
        line_spacing = data.get("line_spacing")
        columns = data.get("columns")
        try:
            # 防御式：分栏数须为 >=1 的整数，否则视为未指定（不因脏数据破坏导出）。
            columns = int(columns) if columns is not None else None
            if columns is not None and columns < 1:
                columns = None
        except (TypeError, ValueError):
            columns = None
        return cls(
            line_spacing=float(line_spacing) if line_spacing is not None else None,
            alignment=alignment,
            first_line_indent=data.get("first_line_indent"),
            font=data.get("font"),
            columns=columns,
        )


# --------------------------------------------------------------------------- #
# 内部辅助
# --------------------------------------------------------------------------- #

def _is_jsonish(value: Any) -> bool:
    """判断一个值是否可直接放入 transcript（基本可序列化类型）。"""
    return isinstance(value, (str, int, float, bool, type(None), list, dict))


__all__ = [
    "WritingTask",
    "AgentSession",
    "TaskResult",
    "TaskAgentConfig",
    "ProposedChange",
    "RejectedChange",
    "GateOutcome",
    "ToolSpec",
    "Typesetting",
    "ALIGNMENT_VALUES",
    "CHANGE_CONTENT",
    "CHANGE_CITATION",
]
