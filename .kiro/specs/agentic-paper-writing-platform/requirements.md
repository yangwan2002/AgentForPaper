# Requirements Document

需求文档：agentic-paper-writing-platform（自然语言驱动的学术论文写作智能体平台）

## Introduction

本特性把现有的学术论文写作系统（`src/paper_agent/`）从「按文件后缀硬路由到两条固定管线（原地润色 / 完整重渲染）」的**路由器架构**，重构为一个**自然语言驱动的学术论文写作智能体平台**。

目标：用户用自然语言下达任意学术写作相关任务（例如「更改实验章节的叙述方式」「更改引言」「增加十篇相关领域的参考文献」「润色某一章节的文字」「排版某个章节的格式」），由一个顶层智能体理解目标、自主编排一批工具、多步推进直至完成，而非只能对整篇初稿做固定润色。系统还需支持未来接入外部工具（MCP、skills），并在整个过程中把学术正确性护栏（忠实性、引用真实性、反幻觉、质量）作为**强制闸门**保留下来。

架构方向（三层，供理解需求背景）：
1. **意图/对话层**：接收用户自由文本目标，不做固定意图分类；无法完成或超出能力时对话式反问/优雅降级，而非报错失败。
2. **编排层（Agent Loop）**：由既有有界 ReAct 工具循环（`agents/tool_loop.py`）升级为顶层调度者，LLM 自主决定调用哪些工具、以何顺序完成目标。「润色初稿」退化为众多可能任务之一。
3. **工具层 + 护栏**：既有工具（`tools/`）与未来的 MCP/skills 工具经统一注册表（`tools/registry.py`）暴露；学术正确性护栏不进入 LLM 自由裁量，而作为工具产出/落盘时的强制闸门。

本特性沿用本代码库既有契约与设施：智能体不直接写工作区，一切写入经「更新意图 → 仓储原子落盘」单一写路径（`agents/base.py`、`orchestrator.py`）；工具注册表导出 OpenAI function-calling schema 并支持 `before/after_tool_call` 钩子；工具循环具备历史压缩、结果截断、token 计量、最大轮数上限与工具错误回灌自纠；澄清经既有 `Elicitor` 抽象（交互 `CLIElicitor` / 非交互 `AutoElicitor` 取默认答案）；外部工具输出与 LLM 输出一律视为不可信数据（防御式处理，不 `eval`/`exec`）；用量与截止时间经 `observability/`（`UsageTracker`）与预算/deadline 闸控制。

### 范围边界（Out of Scope）

- 本特性不实现任何具体的新领域工具（如学术画图、docs 整理）的内部算法；仅定义使这类工具（含 MCP/skills）可被接入与调度的开放接入点。
- 本特性不改变原地润色（`InplaceLatexPolisher`/`InplaceDocxPolisher`）「只改文字、保原排版」的既有语义；它们作为可被智能体选用的能力之一。
- 本特性不实现具体的 pandoc 转换/格式闸细节（由既有 spec `format-pipeline-and-diff-revision` 拥有）与会议模板能力（由 `venue-templates-figures-tables` 拥有）；本特性仅在顶层调度中调用它们。
- 本特性不定义具体 LLM 供应商选择逻辑（由既有 `providers/` 拥有）。

## Glossary

- **Writing_Task**：用户以自由文本自然语言下达的一个学术写作相关目标（如「改引言」「加十篇参考文献」「润色第 3 节」）。
- **Agent_Session**：一次从接收 `Writing_Task` 到产出结果（或诚实反馈无法完成）的智能体运行过程，具备可观测记录与可续跑标识。
- **Agent_Loop**：顶层调度循环，由既有 `agents/tool_loop.py` 升级而来；每轮由 LLM 决定「调用工具」或「给出最终答复」，直至任务完成或触达有界上限。
- **Tool**：可被 `Agent_Loop` 调用的原子能力，经 `Tool_Registry` 注册并以 function-calling schema 暴露。含既有工具与未来接入的 `External_Tool`。
- **External_Tool**：经 MCP 或 skills 机制接入的外部工具（如画图、docs 整理）。
- **Tool_Registry**：工具注册表（`tools/registry.py`），统一注册、按名调度、导出 schema、支持调用前后钩子。
- **Section_Scope_Task**：作用于文档某一章节或局部（而非整篇）的 `Writing_Task`（如「改实验章节叙述」「排版某章节」）。
- **Correctness_Guardrail**：学术正确性护栏，含忠实性校验（`faithfulness_*`）、引用真实性/审计、反幻觉、质量闸（`quality_gate`）。
- **Workspace**：论文工作区（`workspace/models.py` 的 `PaperWorkspace`），任务操作与产出的载体。
- **Mutation_Intent**：对 `Workspace` 的「更新意图」（`agents/base.py` 的 `WorkspaceMutation`），由 Orchestrator 经仓储原子落盘；智能体不直接写工作区。
- **Elicitor**：澄清机制抽象（`elicitation.py`）；`CLIElicitor` 交互式、`AutoElicitor` 非交互（一律取默认答案）、`ScriptedElicitor` 脚本化。
- **Bounded_Limit**：`Agent_Session` 的有界性上限，含最大工具调用轮数、token 预算、wall-clock 截止时间。
- **Capability_Boundary**：系统当前实际具备的能力集合边界（由已注册的 `Tool` 决定）。
- **Legacy_Entry**：既有的「给一个初稿文件或主题」入口用法（`scripts/run_real.py` + `entry.py`）。

## Requirements

### Requirement 1: 接收自然语言写作任务

**User Story:** 作为论文作者，我希望用自然语言下达任意学术写作相关任务，以便系统按我的真实意图工作，而不局限于「润色整篇初稿」。

#### Acceptance Criteria

1. THE 系统 SHALL 提供一个入口，用于接收一段自由文本的 `Writing_Task`。
2. WHEN 用户提交一个 `Writing_Task`，THE 系统 SHALL 发起一个 `Agent_Session` 处理该任务。
3. THE 系统 SHALL 允许 `Writing_Task` 附带零个或多个上下文输入（如现有论文工作区、初稿文件、主题背景、研究数据）。
4. WHEN 用户提交的 `Writing_Task` 为空字符串或仅含空白字符，THE 系统 SHALL 拒绝发起 `Agent_Session` 并提示需要一个任务描述。
5. THE 系统 SHALL 不要求用户预先将任务归类到任何固定处理模式。

### Requirement 2: 智能体自主编排工具完成任务

**User Story:** 作为论文作者，我希望系统自己判断该用哪些工具、按什么顺序完成我的任务，以便我不必了解内部流程。

#### Acceptance Criteria

1. WHEN 一个 `Agent_Session` 开始，THE Agent_Loop SHALL 基于 `Writing_Task` 与当前可用的 `Tool` 集合，自主决定每一步调用哪个 `Tool` 或给出最终答复。
2. THE Agent_Loop SHALL 支持在单个 `Agent_Session` 内多轮调用多个不同的 `Tool` 以完成一个 `Writing_Task`。
3. WHEN 某个 `Tool` 调用返回结果，THE Agent_Loop SHALL 将该结果纳入后续决策的上下文。
4. WHEN 某个 `Tool` 调用失败，THE Agent_Loop SHALL 将失败信息回灌以供后续自我纠正，而不终止整个 `Agent_Session`。
5. THE Agent_Loop SHALL 不依赖按文件扩展名预先选定的固定处理路径来决定工具编排。

### Requirement 3: 章节级与局部任务能力

**User Story:** 作为论文作者，我希望系统能只针对某一章节或局部执行任务，以便我能精细地改我想改的部分而不动其他内容。

#### Acceptance Criteria

1. WHEN `Writing_Task` 指向文档的某一章节或局部（`Section_Scope_Task`），THE 系统 SHALL 将操作限定于该目标范围。
2. WHILE 执行一个 `Section_Scope_Task`，THE 系统 SHALL 保持目标范围之外的既有内容不被改动。
3. IF `Writing_Task` 所指的章节或局部无法在当前 `Workspace` 中被唯一定位，THEN THE 系统 SHALL 经 Elicitor 向用户澄清目标范围，而非擅自选择。
4. THE 系统 SHALL 支持对章节级目标执行改写叙述、润色文字与排版格式类任务。

### Requirement 4: 内容增补类任务与文献护栏结合

**User Story:** 作为论文作者，我希望让系统为我增补内容（如增加相关参考文献），同时保证增补的内容真实可靠，以便我的论文不被引入虚构信息。

#### Acceptance Criteria

1. WHEN `Writing_Task` 要求增补一定数量的参考文献，THE 系统 SHALL 经文献检索类 `Tool` 获取候选文献并将其纳入论文。
2. WHERE 系统向论文增补参考文献，THE 系统 SHALL 对增补的每条文献经引用真实性护栏校验其可核验性。
3. IF 系统无法获取到满足 `Writing_Task` 所要求数量的可核验文献，THEN THE 系统 SHALL 告知用户实际增补的数量与差额原因，而非以虚构文献填充。
4. WHEN `Writing_Task` 要求增补正文内容，THE 系统 SHALL 对增补内容经 `Correctness_Guardrail` 校验后方可落盘。

### Requirement 5: 学术正确性护栏作为强制闸门

**User Story:** 作为论文作者，我希望即使系统自主编排工具，产出仍必须通过学术正确性检查，以便灵活性不以牺牲论文可靠性为代价。

#### Acceptance Criteria

1. WHERE 一次 `Tool` 执行产生对 `Workspace` 的内容改动，THE 系统 SHALL 在该改动落盘前对其施加相关的 `Correctness_Guardrail`。
2. THE 系统 SHALL 不允许 Agent_Loop 的自主决策绕过 `Correctness_Guardrail`。
3. IF 某项内容改动未通过 `Correctness_Guardrail`，THEN THE 系统 SHALL 阻止该改动按原样落盘，并将失败原因回灌给 Agent_Loop 以供修正。
4. WHEN 一个 `Agent_Session` 产出最终结果，THE 系统 SHALL 报告该结果通过与未通过的 `Correctness_Guardrail` 维度。

### Requirement 6: 工作区写入一致性

**User Story:** 作为系统维护者，我希望所有对论文的改动都经统一的原子写路径，以便持久化状态始终一致、可复现。

#### Acceptance Criteria

1. WHERE 一个 `Tool` 需要改动 `Workspace`，THE `Tool` SHALL 产出 `Mutation_Intent` 而非直接写入 `Workspace`。
2. THE 系统 SHALL 经仓储将 `Mutation_Intent` 原子地应用到 `Workspace`。
3. WHILE 应用一批 `Mutation_Intent`，IF 其中任一应用失败，THEN THE 系统 SHALL 保持 `Workspace` 处于一致状态而不留下部分写入。

### Requirement 7: 工具的可扩展接入（MCP / skills）

**User Story:** 作为论文作者，我希望未来能给系统接入更多厉害的工具（如画图、整理 docs），以便持续扩展我的写作大师的能力。

#### Acceptance Criteria

1. THE Tool_Registry SHALL 提供一个统一接入点，用于注册 `External_Tool`（含 MCP 工具与 skills）。
2. WHEN 一个 `External_Tool` 被注册，THE 系统 SHALL 使其对 Agent_Loop 可见并可被调用，无需修改 Agent_Loop 的核心逻辑。
3. THE 系统 SHALL 以与内建 `Tool` 一致的方式向 Agent_Loop 暴露 `External_Tool` 的名称、描述与参数 schema。
4. WHERE 一个 `External_Tool` 调用产生对 `Workspace` 的内容改动，THE 系统 SHALL 对该改动施加与内建工具相同的 `Correctness_Guardrail` 与写入一致性约束。
5. IF 一个已注册的 `External_Tool` 在调用时不可用或出错，THEN THE 系统 SHALL 将该错误按 `Tool` 失败处理并回灌给 Agent_Loop，而不终止 `Agent_Session`。

### Requirement 8: 澄清、优雅降级与诚实反馈

**User Story:** 作为论文作者，我希望系统读不准或做不到时向我确认或如实告知，以便它不会瞎猜或假装完成。

#### Acceptance Criteria

1. IF `Writing_Task` 存在多个同等可能的解读，THEN THE 系统 SHALL 经 Elicitor 向用户澄清，而非擅自选择其一。
2. IF `Writing_Task` 超出当前 `Capability_Boundary`，THEN THE 系统 SHALL 明确告知用户该任务无法完成及其原因，而非报错崩溃或返回虚假结果。
3. WHERE 一个 `Writing_Task` 仅部分可完成，THE 系统 SHALL 告知用户已完成部分与未完成部分。
4. WHILE 运行于非交互模式，THE 系统 SHALL 经 AutoElicitor 对澄清问题取默认答案并继续 `Agent_Session`。
5. WHEN `Agent_Session` 结束，THE 系统 SHALL 告知用户其实际采取的关键处理决策。

### Requirement 9: 会话的有界性、可观测与可续跑

**User Story:** 作为论文作者，我希望任务运行有明确的资源上限、可观察进度、并能从中断处继续，以便不会失控烧钱也不会白跑。

#### Acceptance Criteria

1. THE Agent_Session SHALL 受 `Bounded_Limit`（最大工具调用轮数、token 预算、wall-clock 截止时间）约束。
2. WHEN 一个 `Agent_Session` 触达任一 `Bounded_Limit`，THE 系统 SHALL 停止进一步的工具调用并产出当前可得的最佳结果，同时告知触达了哪个上限。
3. WHILE 一个 `Agent_Session` 运行，THE 系统 SHALL 发出可观测事件记录其工具调用与关键决策。
4. THE 系统 SHALL 为每个 `Agent_Session` 提供一个可续跑标识，允许从既有工作区状态继续。
5. WHEN 用户以某 `Agent_Session` 的续跑标识发起续跑，THE 系统 SHALL 基于该会话已持久化的 `Workspace` 状态继续，而非从零重建。

### Requirement 10: 不可信数据的防御式处理

**User Story:** 作为系统维护者，我希望工具输出与模型输出被当作不可信数据处理，以便系统不被超长或恶意内容破坏。

#### Acceptance Criteria

1. THE 系统 SHALL 将 `Tool` 输出与 LLM 输出一律视为不可信数据。
2. WHEN 一个 `Tool` 返回超过配置上限的结果，THE 系统 SHALL 在纳入上下文前对其截断并附带说明。
3. THE 系统 SHALL 不对 `Tool` 输出或 LLM 输出执行 `eval` 或 `exec`。
4. WHILE `Agent_Session` 的累计上下文超过配置的 token 预算，THE 系统 SHALL 压缩历史以维持在预算内。

### Requirement 11: 向后兼容既有用法

**User Story:** 作为现有用户，我希望原来「给一篇初稿或一个主题」的用法仍然可用，以便我的既有工作流不被破坏。

#### Acceptance Criteria

1. WHERE 用户以 `Legacy_Entry` 方式提供一个初稿文件或一个主题，THE 系统 SHALL 将其作为一个 `Writing_Task` 受理并产出论文结果。
2. WHEN 用户以 `Legacy_Entry` 方式且未附加自然语言任务描述，THE 系统 SHALL 采用与该输入相称的默认处理目标（初稿→修订润色；主题→从零生成）。
3. THE 系统 SHALL 保持既有的原地润色「只改文字、保原排版」语义在被选用时不变。
