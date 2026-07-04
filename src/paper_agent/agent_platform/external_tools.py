"""外部工具接入（MCP / skills）。

统一接入点：把外部能力（MCP server 工具、skills，如学术画图、docs 整理）适配为
与内建工具一致形态的 ``Tool``，注册进 ``ToolRegistry`` 后即对 Agent_Loop 可见可调，
**无需改动循环核心**（Req 7.1/7.2/7.3）。

安全与一致性：
- 外部工具改工作区时，必须经与内建工具相同的护栏闸门 + 单一写路径（Req 7.4）——
  故约定「改工作区的外部工具应返回 ``list[ProposedChange]``」，由本模块统一经
  ``commit`` 落盘；只读外部工具直接返回结果字符串。
- 外部工具不可用/出错，按普通工具失败处理并回灌，不终止会话（Req 7.5）——
  异常在此被捕获转为错误文本（``registry.call`` 亦会捕获，双重保险）。
- 外部工具输出视为不可信数据（由工具循环截断，Req 10.2）。
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from paper_agent.agent_platform.apply import commit
from paper_agent.agent_platform.models import ProposedChange, ToolSpec
from paper_agent.agent_platform.tools.context import ToolContext
from paper_agent.tools.registry import ToolRegistry


@runtime_checkable
class ExternalToolProvider(Protocol):
    """外部工具提供方（MCP server 适配器 / skills 包）。"""

    def discover(self) -> list[ToolSpec]:
        """列出该提供方暴露的工具（名称/描述/参数 schema）。"""
        ...

    def invoke(self, name: str, **kwargs):
        """调用某外部工具。返回值语义：

        - 只读工具：返回可 ``str()`` 的结果；
        - 改工作区工具：返回 ``list[ProposedChange]``，由平台经护栏落盘。
        """
        ...


def _wrap_invoke(
    ctx: ToolContext, provider: ExternalToolProvider, tool_name: str
):
    """把一次外部工具调用包成注册表 handler：统一护栏落盘 + 失败回灌。"""

    def _handler(**kwargs) -> str:
        try:
            result = provider.invoke(tool_name, **kwargs)
        except Exception as exc:  # noqa: BLE001 - 外部失败按工具失败回灌，不中止会话
            ctx.session.record("external_tool", name=tool_name, error=str(exc))
            return f"外部工具 {tool_name} 调用失败：{exc}"

        # 改工作区：必须经与内建工具相同的护栏 + 单一写路径（Req 7.4）。
        if _is_proposed_changes(result):
            outcome = commit(ctx.repo, ctx.workspace, ctx.gate, list(result))
            ctx.session.record(
                "external_tool",
                name=tool_name,
                passed=outcome.passed,
                rejected=len(outcome.rejected),
            )
            return _format_external_outcome(tool_name, outcome)

        # 只读：直接返回结果文本。
        ctx.session.record("external_tool", name=tool_name, readonly=True)
        return str(result)

    return _handler


def _is_proposed_changes(result) -> bool:
    return isinstance(result, list) and all(
        isinstance(x, ProposedChange) for x in result
    )


def _format_external_outcome(tool_name: str, outcome) -> str:
    if outcome.passed and outcome.accepted_mutations:
        msg = f"外部工具 {tool_name} 的改动已通过护栏并落盘。"
    elif outcome.rejected:
        reasons = "；".join(r.reason for r in outcome.rejected)
        msg = f"外部工具 {tool_name} 的改动未通过护栏，未落盘。原因：{reasons}。"
    else:
        msg = f"外部工具 {tool_name} 未产生可落盘的改动。"
    if outcome.notes:
        msg += " " + " ".join(outcome.notes)
    return msg


def register_external_tools(
    registry: ToolRegistry, ctx: ToolContext, provider: ExternalToolProvider
) -> list[str]:
    """把 provider 暴露的工具全部 upsert 进 registry，返回已注册的工具名列表。

    以与内建工具一致的 schema 形态暴露；不改动 Agent_Loop 核心（Req 7.2）。
    """
    registered: list[str] = []
    for spec in provider.discover():
        registry.register(
            name=spec.name,
            description=spec.description,
            handler=_wrap_invoke(ctx, provider, spec.name),
            parameters=spec.parameters_schema,
        )
        registered.append(spec.name)
    return registered


__all__ = ["ExternalToolProvider", "register_external_tools"]
