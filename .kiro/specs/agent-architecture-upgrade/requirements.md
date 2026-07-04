# Requirements Document

需求文档：agent-architecture-upgrade（智能体架构升级）

## Introduction

本特性对现有学术论文写作多智能体系统进行架构强化升级，在不改变既有核心契约（依赖倒置、`Agent`/`AgentResult`/`WorkspaceMutation` 协议、原子持久化与断点续跑、优雅降级、事件/可观测性与用量统计）的前提下，加固「正确性、健壮性、上下文经济性」三条主线。

本需求文档由已批准的设计文档反向派生，覆盖四项按优先级排序的改进：

1. **正确性修复（最高优先级）**：`ReviewAgent` 在生产环境 JSON 解析失败时绝不伪造单调递增的达标分数；区分 Mock 回退路径与生产解析失败路径；编排器仅当最近一次评审被真实解析（`PARSED` 状态）时才判定 `quality_met`；plan/search 的静默 `extract_json` 回退由 `StructuredParser` 统一治理。
2. **LLM Provider 健壮性**：以 `ResilientLLMProvider` 装饰器（指数退避重试 + 抖动、超时、429 `Retry-After` 处理）层叠于 `ObservableLLMProvider` 内；新增可选 `stream()` 接口与 `CancellationToken`，保持 `complete()` 向后兼容。
3. **工具生态扩展**：新增 `read_section`、`read_reference`、锚点定位的 `edit_section`、`run_quality_gate`、`check_citations` 工具并接入 `ToolRegistry`；更多结构化步骤改走 function calling / JSON 模式（`response_format`）。
4. **上下文经济性**：工具循环历史压缩 + 基于真实分词器的 token 计量（`TokenCounter`，`tiktoken` + 启发式回退）+ 工具结果截断。

## Glossary

- **Review_Agent**：评审智能体，对论文草稿评分并产出反馈记录。
- **Orchestrator**：编排器，驱动固定管线与写作—评审反馈循环。
- **Structured_Parser**：结构化输出统一治理组件，负责调用 LLM、解析 JSON、区分回退与失败语义。
- **Resilient_Provider**：`ResilientLLMProvider`，提供重试/超时/限流健壮性的 LLM 装饰器层。
- **Observable_Provider**：`ObservableLLMProvider`，提供事件与用量统计的 LLM 装饰器层。
- **LLM_Provider**：LLM 调用抽象协议，提供 `complete()` 与可选 `stream()`。
- **Cancellation_Token**：协作式取消令牌，供调用方中断流式输出。
- **Token_Counter**：分词器抽象，统一服务上下文裁剪、用量统计与工具循环计量。
- **Tool_Registry**：工具注册表，向模型暴露可调用工具的 function calling schema。
- **Tool_Loop**：`run_tool_loop`，写作智能体使用的有界工具调用循环。
- **Context_Manager**：上下文管理器，按 token 预算裁剪摘要。
- **Section_Edit_Tool**：章节级精确编辑工具（`edit_section`），按锚点定位编辑。
- **Workspace_Repository**：工作区仓储，唯一的原子落盘写入路径。
- **Review_Record**：评审记录，含分数、建议与 `parse_status`。
- **Parse_Status**：评审/解析来源状态，取值 `PARSED` | `MOCK_FALLBACK` | `FAILED`。
- **Retry_Policy**：重试策略，含最大重试次数、退避基数、上限、抖动比例、是否遵循 `Retry-After`。
- **Quality_Gate**：确定性质量闸检查。
- **Citation_Verifier**：引用真实性校验器。
- **Workspace_View**：工作区只读投影，供只读工具按需取材。
- **iteration_limit**：反馈循环的迭代上限。
- **达标阈值**：某评分维度被判为通过所需的最低分数。

## Requirements

### Requirement 1: 评审解析失败不得伪造达标分数

**User Story:** 作为系统维护者，我希望评审智能体在生产环境无法解析模型输出时如实记录失败，以便反馈循环不会因伪造的达标分数而误判论文质量。

#### Acceptance Criteria

1. IF 生产 provider 的评审输出在达到配置的最大重试次数（取值范围 1 至 3 次，默认 2 次）后仍无法解析为含合法 `scores` 字段的 JSON，THEN THE Review_Agent SHALL 返回 `parse_status == FAILED` 的 Review_Record，并将逻辑性、新颖性、论证充分性、语言质量四个评分维度的分数全部置为严格低于达标阈值的评分量表下限值。
2. IF 评审产生 `parse_status == FAILED` 的 Review_Record，THEN THE Review_Record SHALL 包含非空的 `unparsed_reason` 字段，其长度为 1 至 500 个字符，且标识失败类别（解析失败 / `scores` 缺失或非法 / 论文文本为空）。
3. WHEN 待评审论文文本在去除首尾空白字符后长度为 0，THE Review_Agent SHALL 返回 `parse_status == FAILED` 的 Review_Record，且逻辑性、新颖性、论证充分性、语言质量四个维度分数全部严格低于达标阈值。
4. WHEN 评审输出被成功解析为 JSON 且 `scores` 字段中逻辑性、新颖性、论证充分性、语言质量四个维度至少有一个可解析为数值，THE Review_Agent SHALL 返回 `parse_status == PARSED` 的 Review_Record。
5. WHERE provider 被识别为 Mock/测试 provider，IF 评审输出无法解析为含合法 `scores` 字段的 JSON，THEN THE Review_Agent SHALL 返回 `parse_status == MOCK_FALLBACK` 的 Review_Record。
6. IF 评审输出可解析为 JSON 但 `scores` 字段缺失，或四个维度均无法解析为数值，THEN THE Review_Agent SHALL 返回 `parse_status == FAILED` 的 Review_Record，并将四个维度分数全部置为严格低于达标阈值的评分量表下限值。

### Requirement 2: 达标判定蕴含可信评审

**User Story:** 作为系统维护者，我希望编排器仅在评审真实可信时才宣布质量达标，以便终止状态准确反映论文质量。

#### Acceptance Criteria

1. WHEN 反馈循环判定结果为 `"quality_met"`，THE Orchestrator SHALL 确保以下三项可观测条件同时成立：最近一条 Review_Record 的 `parse_status == PARSED`、`unmet_dimensions` 为空集（全部维度得分均不低于各自配置阈值）、`Quality_Gate.passed == true`；任一条件不成立时不返回 `"quality_met"`。
2. IF 最近一条 Review_Record 的 `parse_status` 不为 `PARSED`，THEN THE Orchestrator SHALL 不返回 `"quality_met"`，并保留当前工作区状态。
3. WHEN `ws.review_records` 为空集，THE Orchestrator SHALL 不判定质量达标。
4. WHEN 反馈循环完成一轮写作—评审，THE Orchestrator SHALL 使 `ws.iteration` 相对上一轮恰好递增 1。
5. WHILE `ws.iteration < iteration_limit` 且未满足达标条件，THE Orchestrator SHALL 继续执行下一轮写作—评审。
6. IF 因最近评审不可信而到达 `iteration_limit`，THEN THE Orchestrator SHALL 以 `iteration_limit_unparsed_review` 作为可诊断的终止原因终止。
7. IF 最近评审可信但未达标且到达 `iteration_limit`，THEN THE Orchestrator SHALL 以 `iteration_limit` 作为终止原因终止并返回 `unmet_dimensions`。
8. WHEN 反馈循环以任意原因终止，THE Orchestrator SHALL 在 `iteration_limit` 轮内完成（保证终止性）。

### Requirement 3: 结构化输出统一治理

**User Story:** 作为开发者，我希望所有调用 LLM 并解析 JSON 的步骤经由统一组件处理，以便消除散落的静默 `extract_json` 回退并杜绝伪造的成功结果。

#### Acceptance Criteria

1. WHEN Structured_Parser 请求结构化输出且 provider 支持，THE Structured_Parser SHALL 首先启用 JSON 模式（`response_format` 为 `json_object`）。
2. IF provider 不支持 JSON 模式，THEN THE Structured_Parser SHALL 回退到 `extract_json` 抽取，抽取成功映射为 `status == PARSED`，抽取失败进入失败语义。
3. WHEN 解析成功（可解析为字典且包含全部 `required_keys` 且各键值非空），THE Structured_Parser SHALL 返回 `status == PARSED`，且 `data` 完全来源于 provider 实际返回。
4. IF 输出不可解析、缺少 `required_keys` 或存在键值为空，THEN THE Structured_Parser SHALL 统一判定为解析失败并进入重试/失败语义。
5. IF 解析失败且 `is_mock` 为真，THEN THE Structured_Parser SHALL 返回 `status == MOCK_FALLBACK`。
6. IF 解析失败且 `is_mock` 为假，THEN THE Structured_Parser SHALL 以强约束提示重试至 `max_parse_retries + 1` 次（`max_parse_retries` 取值范围 0 至 5），仍失败则返回 `status == FAILED`。
7. THE Structured_Parser SHALL 在任何情况下都不返回带占位、默认或合成内容的 `PARSED` 结果（`data` 必须完全来源于 provider 实际返回）。
8. IF Structured_Parser 返回 `status == FAILED`，THEN THE Structured_Parser SHALL 不返回 `data`，并附带失败原因。
9. WHERE plan 与 search 步骤产生结构化输出，THE Structured_Parser SHALL 作为其解析路径，按同一回退/失败语义治理。

### Requirement 4: LLM Provider 健壮性（重试/超时/限流）

**User Story:** 作为系统操作者，我希望 LLM 调用在瞬时故障下自动恢复，以便管线在网络波动或限流时仍能稳定完成。

#### Acceptance Criteria

1. THE Resilient_Provider SHALL 作为装饰器层叠在具体 LLM_Provider 之外、Observable_Provider 之内。
2. THE Retry_Policy SHALL 满足 `max_retries >= 0`（默认 3）且 `base_backoff <= max_backoff`（默认 `base_backoff = 1.0s`、`max_backoff = 30.0s`、`jitter = 0.25`）。
3. IF 底层调用抛出可重试异常（超时、连接重置、429、5xx），THEN THE Resilient_Provider SHALL 按退避公式 `min(base_backoff × 2^(k-1), max_backoff)` 叠加 `[0, jitter]` 区间抖动后重试，且单次休眠封顶为 `max_backoff × (1 + jitter)`。
4. IF 底层调用抛出不可重试异常（鉴权失败、400 请求格式错误），THEN THE Resilient_Provider SHALL 立即抛出且恰好调用底层一次。
5. WHEN 收到 429 响应且 `respect_retry_after` 为真且响应含 `Retry-After`，THE Resilient_Provider SHALL 优先按 `Retry-After` 休眠，且休眠时长封顶为 `max_backoff`。
6. IF 收到 429 响应但无 `Retry-After` 或 `respect_retry_after` 为假，THEN THE Resilient_Provider SHALL 回退到退避公式计算休眠时长。
7. THE Resilient_Provider SHALL 使底层调用总次数不超过 `max_retries + 1`。
8. WHEN Resilient_Provider 执行重试，THE Resilient_Provider SHALL 通过 EventSink 发出 `LLM_RETRY` 事件，载荷含重试序号、计划休眠时长与异常类别。
9. IF 流式调用在已产出增量后底层失败，THEN THE Resilient_Provider SHALL 不重试（避免重复输出）。
10. IF 耗尽全部重试仍失败，THEN THE Resilient_Provider SHALL 抛出 `LLMError` 并保留底层原因。

### Requirement 5: 流式输出与取消

**User Story:** 作为调用方，我希望以流式方式获取模型增量输出并可随时取消，以便降低首字延迟并及时止损。

#### Acceptance Criteria

1. THE LLM_Provider SHALL 提供可选的 `stream()` 接口，逐块产出 `StreamChunk`（`kind` 取值 `content` 或 `thinking`）。
2. THE LLM_Provider SHALL 保持 `complete()` 的签名与返回语义与升级前一致。
3. WHERE 具体 provider 未原生实现 `stream()`，THE LLM_Provider SHALL 经 `StreamingMixin` 基于 `complete(on_delta=...)` 适配出 `stream()`。
4. WHEN 收到底层首个增量，THE LLM_Provider SHALL 立即产出首个 `StreamChunk` 而不缓冲全部输出（首字延迟可测）。
5. WHEN `cancel_token.cancelled` 变为真，THE LLM_Provider SHALL 至多再产出 1 个 `StreamChunk` 即停止并关闭底层流，且将取消视为正常终态而不抛出 `LLMError`。
6. WHEN `cancel_token.cancelled` 在首个增量产出前已变为真，THE LLM_Provider SHALL 干净停止且不抛出 `LLMError`。
7. IF 流式在已产出至少一个增量后底层失败，THEN THE LLM_Provider SHALL 保留已产出增量而不回滚。
8. IF 流式在已产出至少一个增量后底层失败，THEN THE Resilient_Provider SHALL 抛出 `LLMError` 且不重试。
9. IF 流式在尚未产出任何增量时失败且错误可重试，THEN THE Resilient_Provider SHALL 按 Retry_Policy 重试。
10. FOR ALL 确定性 mock 下的同一输入，THE LLM_Provider SHALL 使 `stream()` 产出的 content 增量按序拼接结果等于 `complete()` 的 `content`。
11. WHEN 流式调用经过 Observable_Provider，THE Observable_Provider SHALL 对每个增量发出 `LLM_DELTA` 事件。
12. WHEN 流式调用到达终态（含正常完成与取消终止），THE Observable_Provider SHALL 发出恰好一个 `LLM_USAGE` 事件。

### Requirement 6: 扩展工具生态

**User Story:** 作为写作智能体，我希望按需读取工作区、做章节级精确编辑并触发质量检查，以便减少上下文占用并提升编辑准确性。

#### Acceptance Criteria

1. THE Tool_Registry SHALL 注册 `read_section`、`read_reference`、`edit_section`、`run_quality_gate`、`check_citations` 共 5 个工具，并暴露含名称、参数字段及其类型的 function calling schema。
2. WHEN `read_section` 被调用，THE Workspace_View SHALL 返回指定 `section_id` 的章节全文且不变更工作区。
3. WHEN `read_reference` 被调用，THE Workspace_View SHALL 返回指定 `reference_id` 的完整元数据且不变更工作区。
4. WHEN `edit_section` 被调用且 `anchor` 在目标章节唯一命中（命中次数 == 1），THE Section_Edit_Tool SHALL 按 `mode`（`replace` | `insert_after` | `insert_before`）产出 `SectionEdit` 编辑意图。
5. IF `edit_section` 的 `anchor` 在目标章节未命中（命中次数 == 0），THEN THE Section_Edit_Tool SHALL 返回明确错误且不产生任何工作区变更。
6. IF `edit_section` 的 `mode` 不属于受限集合 `{replace, insert_after, insert_before}`，THEN THE Section_Edit_Tool SHALL 返回错误且不变更工作区。
7. WHEN `run_quality_gate` 被调用，THE Quality_Gate SHALL 返回问题清单（可为空）且不变更工作区。
8. WHEN `check_citations` 被调用，THE Citation_Verifier SHALL 返回未通过校验的引用 id 清单（可为空）且不变更工作区。
9. THE Section_Edit_Tool SHALL 仅经 WritingAgent 汇聚为 `WorkspaceMutation` 写入，不直接写工作区。
10. IF `read_section` 的 `section_id` 不存在，THEN THE Workspace_View SHALL 返回错误且不变更工作区。
11. IF `read_reference` 的 `reference_id` 不存在，THEN THE Workspace_View SHALL 返回错误且不变更工作区。
12. IF `edit_section` 的 `anchor` 在目标章节命中多于一处（命中次数 > 1），THEN THE Section_Edit_Tool SHALL 返回错误且不产生任何工作区变更。

### Requirement 7: 真实分词器 token 计量

**User Story:** 作为开发者，我希望用真实分词器统一计量 token，以便上下文裁剪、用量统计与工具循环口径一致。

#### Acceptance Criteria

1. WHERE `tiktoken` 可成功导入（以此为「可用」判定标准），THE Token_Counter SHALL 使用 `TiktokenCounter` 并按模型选择编码。
2. IF 指定模型的编码缺失，THEN THE Token_Counter SHALL 回退到 `cl100k_base` 编码，返回有效的非负计数且不抛异常。
3. IF `tiktoken` 未安装，THEN THE Token_Counter SHALL 回退到 `HeuristicTokenCounter`（约每 2 字符计 1 token、向上取整、非空文本至少计 1 token）且不抛异常。
4. THE Context_Manager SHALL 使用注入的 Token_Counter 按真实 token 预算（默认 1500，取值范围 1 至 200000）裁剪摘要。
5. THE UsageTracker SHALL 复用同一 Token_Counter，仅在缺少 API 真实计数时进行估算并标记。
6. THE Tool_Loop SHALL 使用同一 Token_Counter 进行历史计量与工具结果截断阈值判断。
7. WHEN 工具结果超过截断阈值（默认 2000，取值范围 1 至 100000），THE Tool_Loop SHALL 截断该结果并保留截断标记。

### Requirement 8: 工具循环历史压缩与结果截断

**User Story:** 作为写作智能体，我希望工具循环在上下文增长时自动压缩历史并截断超长工具结果，以便避免无界追加消息导致的上下文溢出。

#### Acceptance Criteria

1. WHILE 工具循环运行，THE Tool_Loop SHALL 在每轮调用 LLM 前用 `counter.count_messages(messages)` 计算当前消息列表的累计 token 数。
2. IF 累计 token 数大于 `context_token_budget`（取值范围 1 至 1,000,000，由配置提供），THEN THE Tool_Loop SHALL 压缩历史，压缩后的消息列表必须保留全部系统提示消息、最近 `keep_recent_turns`（取值范围 1 至 50，由配置提供）轮的原文消息，并将更早轮次替换为单条摘要消息。
3. WHEN 工具循环每轮调用 LLM 前，THE Tool_Loop SHALL 确保 `counter.count_messages(messages) <= context_token_budget`，单条不可再分割的消息（系统提示或最近 `keep_recent_turns` 轮内的单条消息）除外。
4. IF 历史压缩后 `counter.count_messages(messages)` 仍大于 `context_token_budget`，THEN THE Tool_Loop SHALL 继续以当前消息列表调用 LLM，并在 logs 中追加一条标识"已达不可压缩下限"的记录。
5. IF 单个工具结果的 token 数大于 `max_tool_result_tokens`（取值范围 100 至 100,000，由配置提供），THEN THE Tool_Loop SHALL 将该结果截断至不超过 `max_tool_result_tokens` 个 token，并在其后附加一条备注，备注须指明结果已被截断且包含原始 token 数。
6. THE Tool_Loop SHALL 确保任何回灌的 tool 消息（含成功结果与错误文本）的 token 数不超过 `max_tool_result_tokens + len(note)`。
7. IF 工具 handler 执行抛出异常，THEN THE Tool_Loop SHALL 将错误文本作为对应 `tool_call_id` 的 tool 结果回灌供模型自纠，并继续下一轮循环而不终止。
8. WHEN 工具循环达到 `max_iters`（取值范围 1 至 50，默认 4）轮仍未得到无工具调用的最终答案，THE Tool_Loop SHALL 移除工具定义后再调用一次 LLM 以强制产出最终正文，并在 logs 中追加达到上限的记录。

### Requirement 9: 持久化原子性与契约保持

**User Story:** 作为系统架构师，我希望所有新增工具与智能体仍遵循既有持久化契约，以便保持原子落盘、断点续跑与单一写入路径。

#### Acceptance Criteria

1. THE 所有新增工具与智能体 SHALL 仅经 `AgentResult.mutations` 通过 Workspace_Repository 落盘，且不得调用任何绕过 Workspace_Repository 的文件写入接口。
2. WHEN Workspace_Repository 执行落盘，THE Workspace_Repository SHALL 使写入要么完整生效，要么在失败时保持工作区写入前的字节级状态，不留部分写入中间产物。
3. WHEN `edit_section` 产生编辑意图，THE Section_Edit_Tool SHALL 仅修改目标 `section_id` 对应章节，保持未涉及章节字节级不变。
4. IF `edit_section` 的 `section_id` 不存在，THEN THE Section_Edit_Tool SHALL 拒绝编辑、不产生 mutation 并返回错误，工作区保持不变。
5. IF 落盘失败，THEN THE Workspace_Repository SHALL 回滚并恢复到写入前状态，并返回失败错误。
6. WHEN 管线中断后重启，THE Workspace_Repository SHALL 从最近一次成功提交状态恢复，使已完成章节字节级不变且不重复落盘（断点续跑）。

### Requirement 10: 可观测性与优雅降级

**User Story:** 作为系统操作者，我希望升级后的健壮性与解析过程可观测且在失败时优雅降级，以便诊断问题并保证管线尽量完成产出。

#### Acceptance Criteria

1. IF 生产评审输出在达到重试上限后仍无法解析为预期结构化结果，THEN THE Review_Agent SHALL 发出告警事件，事件含失败原因与不超过 500 字符的输出预览片段。
2. WHERE 反馈循环以 `iteration_limit_unparsed_review` 终止，THE Orchestrator SHALL 使用最近一次成功解析的草稿执行导出，产出全部已配置的导出格式且不中止管线（优雅降级）。
3. WHEN Resilient_Provider 执行重试，THE Observable_Provider SHALL 发出 `LLM_RETRY` 事件。
4. WHEN 流式产出增量，THE Observable_Provider SHALL 对每个增量发出 `LLM_DELTA` 事件。
5. WHEN 调用到达终态，THE Observable_Provider SHALL 发出 `LLM_USAGE` 事件。
6. IF 重试或事件日志记录请求内容，THEN THE Resilient_Provider SHALL 不打印 API 密钥或完整请求体，且预览片段不超过 500 字符。
7. THE 工具结果与 LLM 输出 SHALL 被视为不可信数据，截断（保留不超过 8000 字符）与解析均做防御式处理，不执行 `eval`/`exec`。
