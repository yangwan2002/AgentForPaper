"""Part B 数据模型：Content_Contract / Format_Gate / Format_Repair_Loop。

纯 dataclass / Enum、零外部依赖，便于测试与序列化。字段与设计文档
（format-pipeline-and-diff-revision，Data Models 小节）一致。

语义边界约定（由生产逻辑负责施加，此处以注释标注上限）：
- ``ContractViolation.excerpt`` / ``OffendingFragment.excerpt``：≤500 字符。
- ``ToolRunResult.stderr_excerpt``：≤2000 字符。
- ``FormatGateReport.offending_fragments``：至多 10 段。
- ``RepairOutcome.tool_runs``：工具链运行总次数，恒 ≤ ``max_repair_attempts + 1``。

_Requirements: 5.9, 9.4, 10.8, 11.1, 11.6_
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum

from paper_agent.workspace.models import OutputFormat


# --------------------------------------------------------------------------- #
# Content_Contract 相关（Req 5.5/5.6/5.8/5.9）
# --------------------------------------------------------------------------- #


@dataclass
class ContractViolation:
    """内容契约违规诊断项，绝不静默丢弃内容（Req 5.9）。"""

    kind: str  # "unknown_construct" | "unknown_citation" | "unknown_figure" | "length_exceeded"
    message: str  # 可诊断描述
    offset: int | None = None  # 字符偏移（可定位）
    line: int | None = None  # 行号（可定位，与 offset 二选一）
    column: int | None = None  # 列号
    excerpt: str = ""  # 出错片段（≤500 字符）


@dataclass
class NormalizeResult:
    """规范化结果：归一化后的内容 + 诊断项 + 是否发生改写。"""

    content: str  # 归一化后的 Normalized_Markdown（绝不静默丢弃原内容）
    violations: list[ContractViolation] = field(default_factory=list)
    changed: bool = False  # 是否发生归一化改写


# --------------------------------------------------------------------------- #
# Format_Gate 报告（Req 9.4）
# --------------------------------------------------------------------------- #


@dataclass
class ToolRunResult:
    """单个外部工具（pandoc / pdflatex）的运行结果。"""

    tool_name: str  # "pandoc" | "pdflatex"
    exit_code: int | None  # None 表示未运行 / 工具缺失
    stderr_excerpt: str = ""  # 错误消息片段，≤2000 字符（Req 9.4）
    duration_s: float = 0.0
    timed_out: bool = False  # 是否超时（Req 9.7）
    missing: bool = False  # 工具不可用 / 不可执行（Req 9.8）


@dataclass
class OffendingFragment:
    """产物中的出错定位片段，每段 ≤500 字符、最多 10 段（Req 9.4）。"""

    section_id: str | None
    location: str  # 行号或字符偏移（可定位）
    excerpt: str  # ≤500 字符


@dataclass
class FormatGateReport:
    """确定性格式闸报告；``passed`` 可独立赋值。

    判定规则（Req 9.3/9.4）：
    ``passed == all(t.exit_code == 0 for t in tool_results if 参与判定)
    and not any(timed_out) and not missing_tools``。
    """

    passed: bool
    output_format: OutputFormat
    tool_results: list[ToolRunResult] = field(default_factory=list)
    offending_fragments: list[OffendingFragment] = field(default_factory=list)  # ≤10 段
    timeout_used_s: int | None = None
    missing_tools: list[str] = field(default_factory=list)


# --------------------------------------------------------------------------- #
# 修复循环状态（Req 10.8 / 11.1 / 11.6）
# --------------------------------------------------------------------------- #


class RepairTerminalStatus(str, Enum):
    """修复循环终止状态（Req 11.1）。"""

    REPAIRED_WITHIN_LIMIT = "repaired_within_limit"
    REPAIR_EXHAUSTED = "repair_exhausted"


@dataclass
class RepairAttempt:
    """单次修复尝试记录，脱敏（不含密钥 / 请求体，Req 10.8）。"""

    index: int  # 1..max_repair_attempts（Req 10.8）
    tool_error_category: str  # 工具错误类别
    accepted: bool  # 本次修复是否被采纳（合规且写回）
    gate_passed: bool  # 本次重校验是否通过


@dataclass
class RepairOutcome:
    """修复循环结果；仅经 AgentResult.mutations 写回（Req 10.5）。"""

    status: RepairTerminalStatus
    attempts: list[RepairAttempt] = field(default_factory=list)
    tool_runs: int = 0  # 工具链运行总次数，恒 ≤ max_repair_attempts + 1（Req 11.6）
    last_report: FormatGateReport | None = None
    mutations: list = field(default_factory=list)  # 仅经 AgentResult.mutations 写回
