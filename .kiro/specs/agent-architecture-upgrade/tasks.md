# Implementation Plan: agent-architecture-upgrade（智能体架构升级）

## Overview

本计划在既有 `src/paper_agent/` 代码库上做**增量增强**，严格保持既有契约（依赖倒置、`Agent`/`AgentResult`/`WorkspaceMutation` 协议、原子持久化与断点续跑、优雅降级、事件/可观测性与用量统计）。任务按优先级编排：先交付最高优先级的**正确性修复**（评审不可伪造达标），再依次加固健壮性、流式、token 计量、工具循环与工具生态。每个任务都在前序任务之上增量推进，并以「装配接线」收尾，避免出现游离、未集成的代码。

设计含「Correctness Properties」（共 10 条），因此以 `hypothesis` 性质测试 + `pytest` 单元/集成测试覆盖。带 `*` 的子任务为可选测试任务（可跳过以加速 MVP），核心实现子任务不得跳过。

## Tasks

- [x] 1. 基础数据模型与共享设施
  - [x] 1.1 扩展工作区数据模型与新增结构体
    - 在 `workspace/models.py` 新增 `ParseStatus` 枚举（`PARSED` | `MOCK_FALLBACK` | `FAILED`）
    - 为 `ReviewRecord` 增加 `parse_status: ParseStatus = ParseStatus.PARSED` 与 `unparsed_reason: str = ""` 字段，并同步更新 `to_dict`/`from_dict` 序列化（向后兼容旧记录：缺省视为 `PARSED`）
    - 新增 `SectionEdit`（`section_id`/`anchor`/`replacement`/`mode`）、`StreamChunk`（`kind`/`text`）、`RetryPolicy`（`max_retries`/`base_backoff`/`max_backoff`/`jitter`/`respect_retry_after`）数据类
    - _Requirements: 1.1, 1.2, 4.2_
  - [x] 1.2 扩展事件种类
    - 在 `observability/events.py` 的 `EventKind` 中新增 `LLM_RETRY = "llm_retry"`（载荷将含重试序号、计划休眠时长、异常类别）
    - _Requirements: 4.8, 10.3_
  - [x] 1.3 声明测试与可选增强依赖
    - 在 `pyproject.toml` 的 `dev` 可选依赖中加入 `hypothesis`；新增 `tokenizer = ["tiktoken>=0.7"]` 可选 extra（核心零强依赖，`tiktoken` 缺失时回退启发式）
    - _Requirements: 7.1, 7.3_
  - [x]* 1.4 为扩展后的 ReviewRecord 编写序列化往返单元测试
    - 断言含/不含 `parse_status`、`unparsed_reason` 的记录经 `to_dict`/`from_dict` 后语义不变
    - _Requirements: 1.1, 1.2_

- [x] 2. 正确性修复：结构化解析治理 + 评审不可伪造 + 编排器达标守卫
  - [x] 2.1 实现 `StructuredParser` 组件
    - 新建 `src/paper_agent/parsing/structured_parser.py`，实现 `ParseOutcome` 与 `StructuredParser.request_json(messages, *, required_keys, is_mock)`
    - provider 支持时优先启用 JSON 模式（`response_format={"type":"json_object"}`），不支持时回退 `utils/json_parse.extract_json`
    - 解析成功（dict 且含全部 `required_keys` 且键值非空）→ `PARSED`，`data` 完全来源于 provider 实际返回；`is_mock` 为真且解析失败 → `MOCK_FALLBACK`；`is_mock` 为假且失败 → 以强约束提示重试至 `max_parse_retries + 1` 次仍失败 → `FAILED`（不返回 `data`，附失败原因）；任何情况下绝不返回带合成/占位内容的 `PARSED`
    - _Requirements: 3.1, 3.2, 3.3, 3.4, 3.5, 3.6, 3.7, 3.8_
  - [x]* 2.2 为 `StructuredParser` 编写单元测试
    - 覆盖 JSON 模式成功、降级抽取成功、缺 `required_keys`、空键值、Mock 回退、生产重试至上限后 `FAILED` 各路径
    - _Requirements: 3.2, 3.3, 3.4, 3.5, 3.6, 3.7, 3.8_
  - [x] 2.3 重写 `ReviewAgent` 使用 `StructuredParser`（核心正确性修复）
    - 修改 `agents/review_agent.py`：注入 `parser: StructuredParser` 与 `is_mock: bool`，删除「分数随轮数递增」的伪造回退
    - 实现 `_failed_review`（四维度分数置为严格低于达标阈值的量表下限、`parse_status=FAILED`、非空 `unparsed_reason` 标识失败类别）、`_mock_fallback_review`（`parse_status=MOCK_FALLBACK`）、`_build_record_from`（`PARSED`）
    - 空论文文本（去首尾空白后长度 0）走 `_failed_review`；解析为 JSON 但 `scores` 缺失/四维度均不可解析数值亦走 `_failed_review`
    - _Requirements: 1.1, 1.2, 1.3, 1.4, 1.5, 1.6_
  - [x]* 2.4 编写 Property 1 性质测试（评审不可伪造达标）
    - **Property 1: 评审不可伪造达标**
    - **Validates: Requirements 1.1, 1.3, 1.6**
    - 用 `hypothesis` 生成随机「不可解析评审文本」，断言生产 provider 下 `parse_status==FAILED` 且任一维度分数不达阈值
  - [x] 2.5 编排器达标守卫与可诊断终止原因
    - 修改 `orchestrator.py` 的 `_feedback_loop`：仅当最近 `ReviewRecord.parse_status==PARSED` 且 `unmet` 为空且质量闸通过时返回 `"quality_met"`；`ws.review_records` 为空不判达标
    - 到达 `iteration_limit` 时区分终止原因：最近评审不可信 → `iteration_limit_unparsed_review`，否则 → `iteration_limit`（返回 `unmet_dimensions`）；保证每轮 `ws.iteration` 恰增 1 且在上限内终止
    - 在 `_export_phase` 接入优雅降级：以 `iteration_limit_unparsed_review` 终止时仍使用最近一次成功解析草稿执行全部导出格式，不中止管线
    - _Requirements: 2.1, 2.2, 2.3, 2.4, 2.5, 2.6, 2.7, 2.8, 10.1, 10.2_
  - [x]* 2.6 编写 Property 2/3 性质与集成测试
    - **Property 2: 达标蕴含可信评审；Property 3: 终止性**
    - **Validates: Requirements 2.1, 2.2, 2.6, 2.8**
    - 用「会产出不可解析评审」的脚本化 provider 端到端跑 `Orchestrator`，断言不误判 `quality_met` 且以可诊断原因在 `iteration_limit` 内终止
  - [x] 2.7 将 plan / search 结构化解析统一接入 `StructuredParser`
    - 修改 `agents/plan_agent.py` 与 `agents/search_agent.py` 中调用 LLM 解析 JSON 的路径，改走 `StructuredParser`，按同一回退/失败语义治理（消除散落的静默 `extract_json` 回退）
    - _Requirements: 3.9_
  - [x]* 2.8 为 plan / search 解析路径编写单元测试
    - 断言成功解析、Mock 回退、生产失败三类语义与改造前行为一致
    - _Requirements: 3.9_
  - [x] 2.9 检查点 - 确保正确性修复相关测试全部通过
    - Ensure all tests pass, ask the user if questions arise.

- [x] 3. LLM Provider 健壮性层（`ResilientLLMProvider`）
  - [x] 3.1 实现 `ResilientLLMProvider.complete` 与重试策略
    - 新建 `src/paper_agent/providers/llm/resilient.py`，实现装饰器 `complete`，含 `is_retryable`（超时/连接重置/429/5xx 可重试；鉴权/400 不可重试）、`backoff_delay`（指数退避 `min(base*2^k, max)` + `[0,jitter]` 抖动，单次封顶 `max_backoff*(1+jitter)`）、`retry_after_seconds`（429 优先 `Retry-After`，封顶 `max_backoff`）
    - 重试时经 `sink` 发出 `LLM_RETRY` 事件（含重试序号、计划休眠、异常类别）；底层总调用次数 ≤ `max_retries+1`；不可重试错误恰好调用一次并立即抛出；耗尽重试抛 `LLMError` 并保留底层原因；日志/事件不打印密钥或完整请求体（预览 ≤ 500 字符）
    - _Requirements: 4.1, 4.2, 4.3, 4.4, 4.5, 4.6, 4.7, 4.8, 4.10, 10.6_
  - [x]* 3.2 编写 Property 5 性质测试（重试有界且仅对可重试错误）
    - **Property 5: 重试有界且仅对可重试错误**
    - **Validates: Requirements 4.3, 4.4, 4.5, 4.7**
    - 用 `hypothesis` 生成随机异常序列，断言底层调用次数 ≤ `max_retries+1`、不可重试错误恰好 1 次、429 `Retry-After` 行为
  - [x] 3.3 在 `app.build_orchestrator` 装配健壮性层
    - 修改 `app.py`：装配顺序为 `ObservableLLMProvider(ResilientLLMProvider(base, policy, sink), sink, tracker)`，使 Resilient 层叠在具体 provider 外、Observable 内；策略默认 `max_retries=3`、`base_backoff=1.0`、`max_backoff=30.0`、`jitter=0.25`
    - _Requirements: 4.1, 4.2_
  - [x]* 3.4 编写装配栈集成测试
    - 断言 `Observable(Resilient(base))` 三层叠加下，重试触发 `LLM_RETRY` 事件且用量统计齐全
    - _Requirements: 4.8, 10.3_

- [x] 4. 流式接口与取消（保持 `complete()` 向后兼容）
  - [x] 4.1 扩展 `LLMProvider` 协议与取消令牌
    - 修改 `providers/llm/base.py`：引入 `StreamChunk`（复用 models 定义或在此定义并对齐）、`CancellationToken`（`cancel()`/`cancelled`），在 `LLMProvider` 协议新增可选 `stream(messages, *, cancel_token=None, **opts) -> Iterator[StreamChunk]`，保持 `complete()` 签名与返回语义不变
    - _Requirements: 5.1, 5.2_
  - [x] 4.2 实现 `StreamingMixin`
    - 新建 `src/paper_agent/providers/llm/streaming.py`：基于 `complete(on_delta=...)` 适配出 `stream()`，使 `MockLLMProvider` 与现有 provider 无需改动即获流式能力；收到首个增量立即产出首个 `StreamChunk`（不缓冲全部）；每个增量边界检查 `cancel_token`，取消后至多再产出 1 块即停止并视为正常终态（不抛 `LLMError`）；首增量前已取消则干净停止
    - _Requirements: 5.3, 5.4, 5.5, 5.6, 5.10_
  - [x] 4.3 实现 `ResilientLLMProvider.stream`
    - 在 `providers/llm/resilient.py` 增加 `stream`：尚未产出任何增量且错误可重试 → 按 `RetryPolicy` 重试；已产出 ≥1 增量后底层失败 → 抛 `LLMError` 且不重试（避免重复输出）；保留已产出增量不回滚
    - _Requirements: 5.7, 5.8, 5.9_
  - [x] 4.4 实现 `ObservableLLMProvider.stream` 与流式事件
    - 修改 `observability/llm_wrapper.py`：`stream` 逐块转发并对每个增量发 `LLM_DELTA` 事件；到达终态（正常完成或取消）发出恰好一个 `LLM_USAGE` 事件
    - _Requirements: 5.11, 5.12, 10.4, 10.5_
  - [x]* 4.5 编写 Property 4/6 性质测试
    - **Property 4: complete 向后兼容；Property 6: 流式聚合一致**
    - **Validates: Requirements 5.2, 5.10**
    - 同一确定性 mock 下，断言 `complete()` 行为不变、`stream()` content 增量按序拼接等于 `complete().content`

- [x] 5. 真实分词器 token 计量（`TokenCounter`）
  - [x] 5.1 实现 `TokenCounter` 抽象与两种实现
    - 新建 `src/paper_agent/context/tokenizer.py`：`TokenCounter` 协议（`count`/`count_messages`）、`TiktokenCounter`（按模型选编码，缺失编码回退 `cl100k_base`，返回非负计数不抛异常）、`HeuristicTokenCounter`（约 2 字符/token、向上取整、非空文本至少 1 token）、`build_token_counter()`（`tiktoken` 可导入则用 Tiktoken，否则启发式）
    - _Requirements: 7.1, 7.2, 7.3_
  - [x]* 5.2 编写 `TokenCounter` 单元测试
    - 断言编码缺失回退、`tiktoken` 缺失回退、启发式下界与非负计数
    - _Requirements: 7.2, 7.3_
  - [x] 5.3 `UsageTracker` 复用 `TokenCounter`
    - 修改 `observability/usage.py`：以注入的 `TokenCounter` 替换 `len//2` 估算，仅在缺 API 真实计数时估算并标记 `estimated`
    - _Requirements: 7.5_
  - [x] 5.4 `ContextManager` 注入 `TokenCounter`
    - 修改 `context/manager.py`：构造接受 `counter: TokenCounter`，`_select_summaries` 按真实 token 预算（默认 1500，范围 1–200000）裁剪，移除本地 `estimate_tokens` 口径
    - _Requirements: 7.4_
  - [x] 5.5 在装配层注入统一 `TokenCounter`
    - 修改 `app.py`：构造单一 `counter` 并注入 `ContextManager`、`UsageTracker`，供后续工具循环复用，保证全局口径一致
    - _Requirements: 7.5, 7.6_

- [x] 6. 工具循环升级：历史压缩 + 结果截断 + 真实计量
  - [x] 6.1 升级 `run_tool_loop` 与辅助函数
    - 修改 `agents/tool_loop.py`：新增 `ToolLoopConfig`（`max_iters`/`context_token_budget`/`max_tool_result_tokens`/`keep_recent_turns`），`run_tool_loop` 接受 `counter` 与 `config`
    - 每轮调用 LLM 前用 `counter.count_messages` 计量；超 `context_token_budget` 调 `compact_history`（保留全部系统提示 + 最近 `keep_recent_turns` 轮原文 + 旧轮折叠为单条摘要消息），压缩后仍超限则继续并在 logs 记「已达不可压缩下限」
    - 实现 `truncate_to_tokens`：工具结果超 `max_tool_result_tokens` 时截断并附带含原始 token 数的截断备注，保证回灌 tool 消息（含错误文本）token ≤ `max_tool_result_tokens + len(note)`；工具异常将错误文本作为对应 `tool_call_id` 结果回灌且继续；达 `max_iters` 后移除工具再调一次强制收尾并记日志
    - _Requirements: 8.1, 8.2, 8.3, 8.4, 8.5, 8.6, 8.7, 8.8, 7.6, 7.7, 10.7_
  - [x]* 6.2 编写 Property 7 性质测试（上下文预算）
    - **Property 7: 上下文预算**
    - **Validates: Requirements 8.1, 8.3**
    - 用 `hypothesis` 生成随机消息序列，断言每轮调用 LLM 前 `count_messages <= context_token_budget`（单条不可分割消息除外）
  - [x]* 6.3 编写 Property 8 性质测试（工具结果截断）
    - **Property 8: 工具结果截断**
    - **Validates: Requirements 8.5, 8.6**
    - 用 `hypothesis` 生成随机超长工具结果，断言回灌 tool 消息 token ≤ `max_tool_result_tokens + len(note)` 且保留截断标记
  - [x] 6.4 在 `WritingAgent` 接入升级后的工具循环
    - 修改 `agents/writing_agent.py`：向 `run_tool_loop` 传入注入的 `counter` 与 `ToolLoopConfig`，保持现有「写作期按需检索」行为不回归
    - _Requirements: 7.6, 8.1_

- [x] 7. 扩展工具生态（按需读取 + 章节级精确编辑 + 质量/引用检查）
  - [x] 7.1 实现只读工作区访问工具
    - 新建 `src/paper_agent/tools/workspace_tools.py`：`WorkspaceView` 只读投影与 `WorkspaceReadTools.read_section(section_id)`、`read_reference(reference_id)`；命中返回全文/完整元数据且不变更工作区，`section_id`/`reference_id` 不存在时返回明确错误且不变更工作区
    - _Requirements: 6.2, 6.3, 6.10, 6.11_
  - [x] 7.2 实现章节级精确编辑工具
    - 新建 `src/paper_agent/tools/section_edit_tool.py`：`SectionEditTool.edit_section(section_id, anchor, replacement, mode)`；`anchor` 唯一命中（==1）按 `mode∈{replace,insert_after,insert_before}` 产出 `SectionEdit` 意图；未命中（==0）、多处命中（>1）、`mode` 非法或 `section_id` 不存在均返回明确错误且不产生任何工作区变更
    - _Requirements: 6.4, 6.5, 6.6, 6.12, 9.3, 9.4_
  - [x] 7.3 实现质量闸 / 引用检查工具封装
    - 新建 `src/paper_agent/tools/quality_tools.py`：`run_quality_gate`（复用既有 `QualityGate`，返回问题清单且只读）、`check_citations`（复用既有 `CitationVerifier`，返回未通过校验引用 id 清单且只读）
    - _Requirements: 6.7, 6.8_
  - [x]* 7.4 为扩展工具编写单元测试
    - 覆盖 read 命中/不存在、`edit_section` 锚点唯一命中/未命中/多处命中/非法 mode、quality_gate 与 check_citations 只读返回
    - _Requirements: 6.2, 6.3, 6.4, 6.5, 6.6, 6.10, 6.11, 6.12_
  - [x] 7.5 注册工具并在 `WritingAgent` 汇聚 `SectionEdit` 为 `WorkspaceMutation`
    - 修改 `agents/writing_agent.py` 与工具注册：将 `read_section`/`read_reference`/`edit_section`/`run_quality_gate`/`check_citations` 注册进 `ToolRegistry` 并暴露 function calling schema（含名称、参数字段及类型）
    - `WritingAgent` 收集本轮 `SectionEdit`，仅对目标 `section_id` 应用并经 `AgentResult.mutations` 落盘，未涉及章节字节级不变；不直接写工作区，保持仓储为唯一写入路径
    - _Requirements: 6.1, 6.9, 9.1, 9.3_
  - [ ]* 7.6 编写 Property 9/10 性质与集成测试
    - **Property 9: 局部编辑不外溢；Property 10: 持久化原子性保持**
    - **Validates: Requirements 6.9, 9.1, 9.3, 9.4**
    - 断言 `edit_section` 仅改目标章节、锚点未命中无变更，且所有新工具/智能体写入均经 `AgentResult.mutations` 通过 `WorkspaceRepository`（无绕过写入）
  - [x] 7.7 最终检查点 - 确保全部测试通过
    - Ensure all tests pass, ask the user if questions arise.

## Notes

- 带 `*` 的子任务为可选测试任务，可为加速 MVP 跳过；核心实现子任务必须实现。
- 每个任务都引用了具体需求验收条款以保证可追溯。
- 检查点用于增量验证；性质测试（`hypothesis`）验证设计中的 10 条通用正确性属性，单元/集成测试验证具体示例与边界。
- 严格保持既有契约：依赖倒置、`Agent`/`AgentResult`/`WorkspaceMutation` 协议、原子持久化与断点续跑、优雅降级、既有事件/用量统计；`tiktoken` 为可选增强，缺失时回退启发式。

## Task Dependency Graph

```json
{
  "waves": [
    { "id": 0, "tasks": ["1.1", "1.2", "1.3"] },
    { "id": 1, "tasks": ["1.4", "2.1", "4.1", "4.2", "5.1"] },
    { "id": 2, "tasks": ["2.2", "2.3", "2.7", "3.1", "4.4", "5.2", "5.3", "5.4", "6.1", "7.1", "7.2", "7.3"] },
    { "id": 3, "tasks": ["2.4", "2.5", "2.8", "3.2", "3.3", "4.3", "6.2", "6.3", "6.4", "7.4"] },
    { "id": 4, "tasks": ["2.6", "3.4", "4.5", "5.5", "7.5"] },
    { "id": 5, "tasks": ["7.6"] }
  ]
}
```
