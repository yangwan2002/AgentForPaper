# Requirements Document

需求文档：agent-observability-tracing（可观测与可追踪）

## Introduction

现有 `EventSink` 已在各处埋点（阶段/LLM 请求响应/token/重试/降级），但只接了终端渲染器
`ConsoleReporter`，事件是**扁平的、实时的、跑完即逝**——无法回答"这次运行/这轮对话里，
是哪一步出错或效果不好"。本特性在**不推倒现有可观测层**的前提下，补齐三样能力：

1. **可追踪（trace/span 关联）**：把一次运行/一轮对话内的所有事件用 `trace_id` 归拢，
   把 LLM 调用、工具调用等串成带父子关系与耗时的 span 树。
2. **可持久化（结构化落盘）**：事件以 JSONL 落盘，一次 trace 一份，可事后检索/回放/归因。
3. **可归因（内容级记录 + 离线查看）**：记录每步的输入/输出内容与关键指标（token/耗时/
   评审分/护栏结果），并提供离线查看工具按 trace 时间线还原、高亮异常。

设计约束（全程遵守）：**加法式、非侵入、失败不影响主流程、默认不改变既有行为**；对外部
追踪平台（Langfuse/OpenTelemetry）保留接入点但不作为本期强依赖。

## Glossary

- **Trace（追踪）**：一次任务运行或一轮对话内所有事件的集合，由唯一 `trace_id` 归拢。
- **Span（跨度）**：trace 内的一个可计时工作单元（如一次 LLM 调用、一次工具调用），带
  `span_id` 与 `parent_span_id`，构成 span 树。
- **Sink（接收器）**：`EventSink` 抽象的实现，决定事件如何被消费（终端渲染 / JSONL 落盘 /
  外部平台）。
- **内容级别（content level）**：落盘 trace 记录内容的详略：`full` / `redacted` / `off`。
- **JSONL**：JSON Lines，每行一个合法 JSON 对象的文本文件格式。

## Requirements

### Requirement 1: Trace/Span 关联

**User Story:** 作为运维/开发者，我希望一次运行内的所有事件能被归到同一条 trace 并串成
调用树，这样我能看清"这一步的上一步是什么、各步耗时多少"。

#### Acceptance Criteria
1. WHEN 一次任务运行（`run_task`）或一轮对话（`ChatController.send`）开始 THE SYSTEM SHALL 为其分配一个唯一 `trace_id`，并使该运行内发出的所有事件携带同一 `trace_id`。
2. WHEN 一次 LLM 调用或一次工具调用发生 THE SYSTEM SHALL 为其开启一个 span，分配 `span_id` 并记录其 `parent_span_id`（无父则为空）。
3. WHEN 一个 span 结束（正常或异常） THE SYSTEM SHALL 记录其 `duration_ms`。
4. WHERE 未开启追踪 THE SYSTEM SHALL 使事件的 trace/span 字段为空且不改变既有事件渲染行为。

### Requirement 2: 持久化落盘

**User Story:** 作为开发者，我希望每次运行的事件能落盘成可解析的结构化文件，跑完之后还能翻看。

#### Acceptance Criteria
1. WHEN 追踪开启 THE SYSTEM SHALL 把事件逐条以 JSON Lines（每行一个合法 JSON 对象）落盘。
2. THE SYSTEM SHALL 使每条记录至少包含 `ts`（时间戳）、`trace_id`、`span_id`、`parent_span_id`、`kind`、`message`、`data`。
3. WHERE 未指定落盘路径 THE SYSTEM SHALL 默认落到工作区目录下的 trace 子目录（且该目录被 gitignore 兜底）。
4. WHEN 落盘发生任何 I/O 异常 THE SYSTEM SHALL 吞掉异常且不中断主流程。

### Requirement 3: 内容级记录与脱敏级别

**User Story:** 作为开发者，我要能看到每步喂进去/吐出来的完整内容来归因"效果不好"，同时
在有合规要求时能脱敏。

#### Acceptance Criteria
1. THE SYSTEM SHALL 提供内容记录级别配置：`full`（全量）/ `redacted`（按长度截断/脱敏）/ `off`（不记内容）。
2. WHERE 级别为 `full` THE SYSTEM SHALL 在落盘 trace 中记录 LLM 请求与响应的完整内容。
3. WHERE 级别为 `redacted` THE SYSTEM SHALL 对落盘内容按配置长度截断/脱敏。
4. THE SYSTEM SHALL 使"给用户看的实时渲染（脱敏 preview）"与"落盘 trace 的内容级别"相互独立配置。

### Requirement 4: 不影响主流程

**User Story:** 作为开发者，我要确保加了追踪不会拖慢或拖垮 agent。

#### Acceptance Criteria
1. WHEN 任一 sink 或追踪组件抛出异常 THE SYSTEM SHALL 捕获并忽略，不向业务传播。
2. THE SYSTEM SHALL 使 span 在被追踪代码抛异常时仍闭合并记录耗时（try/finally 语义）。
3. THE SYSTEM SHALL 使多 sink 分发中，单个 sink 失败不影响其余 sink 收到事件。

### Requirement 5: 向后兼容

**User Story:** 作为维护者，我要确保未开启追踪时系统行为与现状逐字节一致。

#### Acceptance Criteria
1. WHERE 追踪未开启 THE SYSTEM SHALL 保持既有事件流与渲染行为不变。
2. THE SYSTEM SHALL 使 `Event` 新增字段全部可选且默认空，既有构造与断言不受影响。
3. THE SYSTEM SHALL 使既有仅传单一 sink 的装配路径继续可用。

### Requirement 6: 离线归因查看

**User Story:** 作为开发者，出了问题我要能快速定位是哪一步。

#### Acceptance Criteria
1. THE SYSTEM SHALL 提供一个离线查看工具，读取一份 trace JSONL 并按时间线还原各 span。
2. THE SYSTEM SHALL 在查看输出中高亮异常信号：错误、降级（DEGRADATION）、重试（LLM_RETRY）。
3. THE SYSTEM SHALL 在查看输出中汇总关键指标：总耗时、总 token、LLM 调用数、工具调用数。

### Requirement 7: 外部后端接入点

**User Story:** 作为开发者，我希望将来能把 trace 送到 Langfuse/OTel 这类平台，而不改业务代码。

#### Acceptance Criteria
1. THE SYSTEM SHALL 使新增追踪能力完全经 `EventSink` 抽象实现，业务代码不感知具体后端。
2. WHERE 未来实现外部后端 sink THE SYSTEM SHALL 允许其作为又一个 sink 挂入多 sink 分发，无需改动 agent 代码。
