"""智能体平台（agentic-paper-writing-platform）。

把「按文件后缀选固定管线」的路由器架构，升级为「自然语言驱动、自主编排工具」
的顶层智能体平台。分层：

- ``models``        —— 平台数据模型（任务/会话/结果/护栏产物/排版规格等）。
- ``guardrail_gate``—— 学术正确性护栏闸门（强制，不可绕过）。
- ``apply``         —— 通过闸门后的单一原子写路径。
- ``tools/``        —— 把既有能力封装为统一 Tool。
- ``task_agent``    —— 顶层有界工具循环（Agent_Loop）。
- ``intake``        —— 意图/对话层，受理自然语言任务。
- ``external_tools``—— MCP / skills 外部工具接入。

设计契约（沿用既有代码库）：智能体不直接写工作区，一切写入经
「更新意图 → 护栏闸门 → 仓储原子落盘」单一写路径；工具与 LLM 输出视为不可信数据。
"""

from __future__ import annotations
