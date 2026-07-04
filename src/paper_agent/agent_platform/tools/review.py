"""review_paper 工具：按需只读评审（Task 5）。

用户请求"帮我评审/审阅这篇论文"时，Top_Agent 调用本工具。它复用既有
``ReviewAgent``（四维度评分 + 建议）与可选 ``AdversarialReviewAgent``（默认 reject
立场列具体弱点）的判定逻辑，返回**文本报告**，**绝不产生任何工作区 Mutation**
（Property 6：调用前后工作区字节不变）。

只读保证（结构性而非约定）：评审在工作区的**深拷贝**上进行——真实工作区从不传入
评审智能体，其 mutation 只应用到副本后被丢弃。故无论底层评审如何 append 记录，
真实工作区都不受影响。

定位取舍：本工具是"按需评审"，只在用户明确请求评审时由 Top_Agent 调用；定点编辑
/排版/导出等任务不触发（约束经系统提示表达）。它与"确定性收尾验收"互补——后者守
可测正确性（自动、无 LLM），前者补主观质量（按需、有 LLM）。
"""

from __future__ import annotations

import copy

from paper_agent.agent_platform.tools.context import ToolContext
from paper_agent.agents.base import AgentContext
from paper_agent.agents.review_agent import ReviewAgent
from paper_agent.providers.llm.base import LLMProvider
from paper_agent.tools.registry import ToolRegistry
from paper_agent.workspace.models import (
    AdversarialReviewRecord,
    PaperWorkspace,
    ReviewRecord,
)

_REVIEW_SCHEMA = {"type": "object", "properties": {}, "required": []}

_REVIEW_DESCRIPTION = (
    "对当前论文做整体评审（四维度评分 + 具体问题 + 改进建议）。只读工具：只返回评审"
    "报告文本，不修改论文。仅在用户明确请求评审/审阅时调用；定点编辑、排版或导出任务"
    "不要调用它。"
)


def _copy_workspace(ws: PaperWorkspace) -> PaperWorkspace:
    """深拷贝工作区（经 to_dict/from_dict 往返），保证评审只作用于副本。"""
    return PaperWorkspace.from_dict(copy.deepcopy(ws.to_dict()))


def _run_on_copy(agent, ws_copy: PaperWorkspace):
    """在副本上运行一个评审智能体并把其 mutation 应用到副本，返回运行结果。"""
    result = agent.run(AgentContext(workspace=ws_copy))
    for mutate in result.mutations:
        mutate(ws_copy)
    return result


def _format_review_record(record: ReviewRecord) -> str:
    lines = ["## 评审评分"]
    if record.scores:
        for dim, score in record.scores.items():
            lines.append(f"- {dim.value}：{score:.1f}")
    else:
        lines.append("- （本次评审未产出可用评分）")
    if record.suggestions:
        lines.append("\n## 改进建议")
        for dim, text in record.suggestions.items():
            lines.append(f"- {dim.value}：{text}")
    if record.section_feedback:
        lines.append("\n## 章节反馈")
        for sid, text in record.section_feedback.items():
            lines.append(f"- [{sid}] {text}")
    return "\n".join(lines)


def _format_adversarial_record(record: AdversarialReviewRecord) -> str:
    lines = [f"\n## 对抗式评审（结论：{record.decision}）"]
    if record.weaknesses:
        for w in record.weaknesses:
            sid = w.get("section_id", "")
            sev = w.get("severity", "")
            issue = w.get("issue", "")
            fix = w.get("suggested_fix", "")
            loc = f"[{sid}] " if sid else ""
            lines.append(f"- {loc}（{sev}）{issue}" + (f" — 建议：{fix}" if fix else ""))
    else:
        lines.append("- 未列出具体弱点。")
    return "\n".join(lines)


def _handle_review(
    ctx: ToolContext,
    llm: LLMProvider,
    adversarial_llm: LLMProvider | None,
) -> str:
    ws_copy = _copy_workspace(ctx.workspace)

    result = _run_on_copy(ReviewAgent(llm), ws_copy)
    record = ws_copy.review_records[-1] if ws_copy.review_records else None
    if record is None:
        return "评审不可用：未能产出评审记录（论文可能为空或评审输出无法解析）。"

    report = _format_review_record(record)

    # 可选：对抗式评审（独立 LLM 破自评偏置）。失败不阻断主评审报告。
    if adversarial_llm is not None:
        try:
            from paper_agent.agents.adversarial_review_agent import (
                AdversarialReviewAgent,
            )

            _run_on_copy(AdversarialReviewAgent(adversarial_llm), ws_copy)
            if ws_copy.adversarial_records:
                report += "\n" + _format_adversarial_record(
                    ws_copy.adversarial_records[-1]
                )
        except Exception:  # noqa: BLE001 - 对抗审失败不影响主评审
            pass

    ctx.session.record("review_paper", scores={d.value: s for d, s in record.scores.items()})
    return report


def register_review_paper(
    registry: ToolRegistry,
    ctx: ToolContext,
    llm: LLMProvider,
    adversarial_llm: LLMProvider | None = None,
) -> None:
    """把只读评审工具 review_paper 注册进 registry。"""
    registry.register(
        name="review_paper",
        description=_REVIEW_DESCRIPTION,
        handler=lambda: _handle_review(ctx, llm, adversarial_llm),
        parameters=_REVIEW_SCHEMA,
    )


__all__ = ["register_review_paper"]
