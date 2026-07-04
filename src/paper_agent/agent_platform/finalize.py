"""任务收尾验收：解析可测约束 → 运行有界自愈闭环 → 诚实上报（Task 4）。

把确定性验收层（``acceptance``）接入 Top_Agent 的收尾阶段：

1. :func:`parse_requirements`：从任务指令 + 工作区**保守地**解析可测约束（输出格式、
   排版规格、文献数量/年限）。解析不出任何约束 → 返回空 :class:`TaskRequirements`
   （``has_any()`` 为假），收尾**不做多余验收**，既有路径逐字节不变（Property 9）。
2. :func:`make_acceptance_finalizer`：构造一个"收尾器"闭包，交给 ``TaskAgent`` 在
   工具循环自然收尾后调用——导出→验收→（对可自愈项让 Top_Agent 修正重验）→把
   已满足/未满足项及原因写入 ``TaskResult``，绝不静默交付坏结果。

设计取舍：自愈修正经 ``TaskAgent.converse`` 在**同一对话消息序列**上进行，故其改动
仍走既有工具→护栏→单一写路径（本模块不直接写工作区）。乱码等环境/编码类为不可
自愈，直接进入 ``unfinished`` 上报。
"""

from __future__ import annotations

import re
from datetime import datetime, timezone

from paper_agent.agent_platform.acceptance import (
    AcceptanceChecker,
    AcceptanceFinding,
    AcceptanceLoop,
    TaskRequirements,
)
from paper_agent.agent_platform.models import Typesetting
from paper_agent.agent_platform.tools.export_tool import export_paper_files
from paper_agent.workspace.models import OutputFormat, PaperWorkspace

# 指令中的格式关键词 → OutputFormat（小写子串匹配）。
_FORMAT_KEYWORDS = {
    "docx": OutputFormat.DOCX,
    "word": OutputFormat.DOCX,
    ".doc": OutputFormat.DOCX,
    "latex": OutputFormat.LATEX,
    "tex": OutputFormat.LATEX,
    "markdown": OutputFormat.MARKDOWN,
    ".md": OutputFormat.MARKDOWN,
}


def _detect_format(instruction: str) -> OutputFormat | None:
    """从指令探测期望输出格式；无明确格式关键词返回 ``None``。"""
    low = (instruction or "").lower()
    for keyword, fmt in _FORMAT_KEYWORDS.items():
        if keyword in low:
            return fmt
    return None


def _detect_reference_bounds(instruction: str) -> tuple[int | None, int | None]:
    """解析文献数量下/上限（保守：仅匹配明确措辞，否则 None）。"""
    text = instruction or ""
    lo = hi = None
    m = re.search(r"(?:至少|不少于|不低于)\s*(\d+)\s*篇", text)
    if m:
        lo = int(m.group(1))
    m = re.search(r"(?:最多|不超过|不多于)\s*(\d+)\s*篇", text)
    if m:
        hi = int(m.group(1))
    return lo, hi


def _detect_min_year(instruction: str) -> int | None:
    """解析文献最早年限（"近N年" / "20XX年以来"）；无则 None。"""
    text = instruction or ""
    m = re.search(r"近\s*(\d+)\s*年", text)
    if m:
        current = datetime.now(timezone.utc).year
        return current - int(m.group(1)) + 1
    m = re.search(r"(20\d{2})\s*年\s*(?:以来|以后|之后|至今)", text)
    if m:
        return int(m.group(1))
    return None


def parse_requirements(instruction: str, ws: PaperWorkspace) -> TaskRequirements:
    """从任务指令 + 工作区**保守地**解析可测约束。

    只解析措辞明确的约束（不臆测）：
    - 输出格式：指令含 docx/word/latex/markdown 等关键词。
    - 排版规格：工作区已保存 ``profile['typesetting']``（由 set_typesetting 落定）。
    - 文献数量/年限：明确措辞（"至少N篇" / "近N年" / "20XX年以来"）。
    - 引用闭合：仅当存在具体交付格式时才核对（避免对无产出的探索性对话强加验收）。

    未解析出任何约束 → ``has_any()`` 为假，收尾不做多余验收（Property 9）。
    """
    fmt = _detect_format(instruction)

    typeset = None
    spec_data = ws.profile.get("typesetting") if getattr(ws, "profile", None) else None
    if spec_data:
        spec = Typesetting.from_dict(spec_data)
        if not spec.is_empty():
            typeset = spec

    lo, hi = _detect_reference_bounds(instruction)
    min_year = _detect_min_year(instruction)

    # 引用闭合只在有具体交付格式时核对（有成稿产出才谈"参考文献表是否闭合"）。
    require_closure = fmt is not None

    return TaskRequirements(
        expected_format=fmt.value if fmt is not None else None,
        typesetting=typeset,
        reference_count_min=lo,
        reference_count_max=hi,
        min_year=min_year,
        require_citation_closure=require_closure,
    )


def _resolve_export_format(requirements: TaskRequirements, ws: PaperWorkspace) -> OutputFormat:
    """收尾导出格式：优先期望格式，否则工作区当前输出格式。"""
    if requirements.expected_format:
        try:
            return OutputFormat(requirements.expected_format)
        except ValueError:
            pass
    return ws.output_format


def _build_heal_prompt(findings: list[AcceptanceFinding]) -> str:
    """把可自愈的验收发现汇聚为一条给 Top_Agent 的修正指令。"""
    lines = [
        "系统对刚才的产出做了确定性验收，发现以下**可修正**问题，请针对性修正后无需重新导出（系统会自动重导出复验）：",
    ]
    for f in findings:
        lines.append(f"- [{f.check}] {f.detail}")
    lines.append(
        "请只修正上述问题；不要编造或篡改文献信息，不确定的如实说明。"
    )
    return "\n".join(lines)


def run_acceptance(
    agent,
    session,
    messages,
    *,
    instruction: str,
    output_dir: str,
    max_heal_rounds: int = 2,
):
    """对一段（已由 ``messages`` 承载的）对话跑一次收尾验收 + 有界自愈。

    据 ``instruction`` 解析可测约束（``run_task`` 用整体任务描述；chat 用本轮用户消息）。
    解析不出任何可测约束时返回 ``None``（no-op，保持既有路径行为）。否则导出→验收→
    对可自愈项让 ``agent`` 在**同一 messages** 上修正重验，返回 :class:`DeliveryOutcome`。
    """
    ws = session.workspace
    requirements = parse_requirements(instruction, ws)
    if not requirements.has_any():
        return None

    checker = AcceptanceChecker()

    def export_fn(workspace: PaperWorkspace) -> list[str]:
        fmt = _resolve_export_format(requirements, workspace)
        files, _note = export_paper_files(workspace, output_dir, fmt)
        return files

    def heal_fn(sess, findings: list[AcceptanceFinding]) -> None:
        # 让 Top_Agent 在同一对话上针对性修正（改动经工具→护栏→单一写路径）。
        agent.converse(sess, messages, _build_heal_prompt(findings))

    loop = AcceptanceLoop(checker, export_fn, heal_fn)
    outcome = loop.run(session, requirements, max_heal_rounds=max_heal_rounds)
    session.record(
        "acceptance",
        delivered=outcome.delivered,
        healed=list(outcome.healed),
        report=outcome.report.to_dict(),
    )
    return outcome


def make_acceptance_finalizer(output_dir: str, *, max_heal_rounds: int = 2, enabled: bool = True):
    """构造交给 ``TaskAgent`` 的收尾器：``finalizer(agent, session, messages, result)``。

    ``enabled=False`` 或解析不出可测约束时，原样返回 ``result``（行为不变）。
    """

    def finalizer(agent, session, messages, result):
        if not enabled:
            return result
        outcome = run_acceptance(
            agent, session, messages,
            instruction=session.task.instruction,
            output_dir=output_dir,
            max_heal_rounds=max_heal_rounds,
        )
        if outcome is None:
            return result

        # 把验收结论写回 TaskResult（诚实上报已满足/未满足）。
        for finding in outcome.report.findings:
            label = f"[{finding.check}] {finding.detail}"
            if finding.ok:
                result.completed.append(label)
            else:
                result.unfinished.append(label)
        for path in outcome.export_files:
            if path not in result.export_files:
                result.export_files.append(path)
        result.guardrail_report["acceptance_passed"] = outcome.delivered
        result.guardrail_report["acceptance_heal_rounds"] = outcome.heal_rounds
        return result

    return finalizer


def format_acceptance_note(outcome) -> str:
    """把一次验收结论渲染成给用户的简洁提示（chat 路径附加到助手答复后）。

    - 全通过：一行 ✓。
    - 有未解决项：逐条列出（诚实上报，绝不静默）。
    - 有已自愈项：附一行说明。
    """
    if outcome is None:
        return ""
    lines: list[str] = []
    if outcome.healed:
        lines.append(f"（收尾验收：已自愈 {len(outcome.healed)} 项：{'、'.join(dict.fromkeys(outcome.healed))}）")
    if outcome.delivered:
        lines.append("✓ 收尾验收通过（乱码/排版/引用闭合/数量年限等可测项均达标）。")
    else:
        lines.append("⚠ 收尾验收发现以下未解决问题（已如实上报，未静默交付）：")
        for detail in outcome.unresolved:
            lines.append(f"  - {detail}")
    return "\n".join(lines)


__all__ = [
    "parse_requirements",
    "run_acceptance",
    "make_acceptance_finalizer",
    "format_acceptance_note",
]
