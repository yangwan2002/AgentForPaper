"""run_full_pipeline 复合工具：把「从主题写整篇 / 整篇重渲染」交给既有完整管线。

设计取舍：重任务（规划→检索→写审循环→导出→护栏）整段复用既有 ``Orchestrator``，
零回归（设计 Property 9）。本工具不重实现管线，只：
1. 经注入的 ``runner`` 触发管线在**当前会话工作区**上运行（同一 store，走 resume 语义）；
2. 跑完把更新后的工作区回填到会话；
3. 汇报终止原因、可投递性与产出文件。

依赖注入：``runner: Callable[[str], PaperResult]``（通常是 ``lambda wid:
orchestrator.run(resume_id=wid)``）在装配时注入，使本模块不直接依赖 Orchestrator，
便于测试与解耦。
"""

from __future__ import annotations

from typing import Callable

from paper_agent.agent_platform.tools.context import ToolContext
from paper_agent.tools.registry import ToolRegistry

# 管线运行器：入参为工作区 id，返回带 export/terminated_reason/submittable 的结果对象。
PipelineRunner = Callable[[str], object]

_PIPELINE_SCHEMA = {
    "type": "object",
    "properties": {},
    "required": [],
}

_PIPELINE_DESCRIPTION = (
    "运行完整论文写作管线：适用于「从主题从零撰写整篇论文」或「对整篇初稿做完整"
    "重渲染式修订」这类重任务。会自动完成规划、文献检索、写作—评审多轮迭代、"
    "质量与忠实性把关及导出。不要用它做单章节的小改动（那应使用章节级工具）。"
)


def _handle_run_pipeline(ctx: ToolContext, runner: PipelineRunner) -> str:
    workspace_id = ctx.session.workspace.workspace_id
    result = runner(workspace_id)

    # 管线经同一 store 落盘；回填最新工作区到会话，保证后续工具看到最新内容。
    reloaded = ctx.repo.load(workspace_id)
    if reloaded is not None:
        ctx.session.workspace = reloaded

    return _format_pipeline_result(ctx, result)


def _format_pipeline_result(ctx: ToolContext, result) -> str:
    reason = getattr(result, "terminated_reason", "")
    submittable = getattr(result, "submittable", None)
    export = getattr(result, "export", None)
    files = list(getattr(export, "files", []) or [])

    ctx.session.record(
        "run_full_pipeline",
        terminated_reason=reason,
        submittable=submittable,
        files=files,
    )

    parts = [f"完整管线已运行（终止原因：{reason or '未知'}）。"]
    if submittable is not None:
        parts.append(f"可投递：{'是' if submittable else '否'}。")
    if files:
        parts.append("产出文件：" + "、".join(files) + "。")
    return " ".join(parts)


def register_run_full_pipeline(
    registry: ToolRegistry, ctx: ToolContext, runner: PipelineRunner
) -> None:
    """注册 run_full_pipeline 工具（管线运行器经 ``runner`` 注入）。"""
    registry.register(
        name="run_full_pipeline",
        description=_PIPELINE_DESCRIPTION,
        handler=lambda: _handle_run_pipeline(ctx, runner),
        parameters=_PIPELINE_SCHEMA,
    )


__all__ = ["register_run_full_pipeline", "PipelineRunner"]
