"""平台工具层：把既有能力封装为统一 Tool，经 ``ToolRegistry`` 暴露给 Agent_Loop。

分两类：
- **只读工具**（``locate`` / ``export_tool`` / ``ask``）：不改工作区，直接返回信息。
- **改工作区工具**（后续任务）：只产出 ``ProposedChange``，经 ``apply.commit`` 单一
  写路径落盘，绝不直接写工作区。

所有工具通过共享的 ``ToolContext`` 获取运行期依赖（工作区、仓储、护栏闸门、
澄清器、输出目录），使工具函数保持无状态、可测试。
"""

from __future__ import annotations
