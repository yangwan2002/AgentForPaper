# Requirements Document

需求文档：agent-reliability-and-subagents（输出验收、按需评审与选择性子智能体）

## Introduction

本特性是 `agentic-paper-writing-platform` 之上的**可靠性与架构演进**，把真机使用中暴露的一批问题与讨论收敛为可执行需求。核心目标：**让平台的最终产出可被验收、坏结果一定被发现并如实上报，并在收益最大处引入子智能体（而非全量 fork）**，从而提升鲁棒性与可扩展性，同时不牺牲长文写作所需的全局理解。

三条设计主线（源于与用户的评估结论）：

1. **输出验收 + 诚实上报（而非"万能自愈"）**：任务完成前，对照用户可测需求做**确定性验收**（格式是否应用、docx 是否可读无乱码、引用是否闭合、文献数量/年限等）；能自愈的（工具/内容层，如换工具、按护栏原因改写重提）走**有界自愈**，不能自愈的（环境/代码/依赖问题）**尽早检测、明确上报**，绝不静默交付坏结果，也绝不无界重试。
2. **按需 LLM 评审**：把主观质量评审（逻辑严谨性、写作质量）做成**按需调用的只读工具/子智能体**，用户/顶层智能体在需要时触发，返回评审报告，不自动改稿。
3. **选择性子智能体（混合架构）**：顶层仍以"直接调工具"为主；**仅在"隔离即优点"的场景** fork 子智能体——独立评审、并行独立查询（如并行核验大量文献）；**章节写作不做纯隔离**，维持"共享工作区（唯一真相源）+ 精选上下文（全局摘要/术语表/邻居摘要/目标章节全文）"的既有做法，避免因缺全局上下文而降质。

### 触发本特性的真实证据（来自真机使用）

- docx 经 pandoc 导出时因子进程未用 UTF-8 产生乱码，且**静默交付**给用户（缺输出验收）。
- `add_references` 累积近 80 篇候选，但导出的参考文献表**全量列出**（含从未被正文引用者），与"约 20 篇"的需求不符（缺引用闭合验收）。
- 顶层单一循环在长论文任务上上下文膨胀，出现"卡住/撞轮数上限"（缺上下文隔离手段）。
- 增量路径无 LLM 评审，用户无法按需获得质量评审。

### 范围边界（Out of Scope）

- 不改变既有护栏"守正确性不管完整性"的定位（本特性的验收是**附加的可测核对**，不是把完整性变成硬拦截）。
- 不实现具体 PDF 公式识别/OCR。
- 不做网页版/多用户服务化（属更后期）。
- 不把"写→审"重新固化为对所有需求强制的固定流程（明确反对全量 fork）。

## Glossary

- **Top_Agent**：顶层智能体循环（`TaskAgent`），接收自然语言任务并编排工具/子智能体。
- **Acceptance_Check**：输出验收——对照用户可测需求核对最终产出的确定性检查集合。
- **Deterministic_Check**：不依赖 LLM 的可精确判定检查（如乱码检测、格式是否应用、引用闭合、数量核对）。
- **Mojibake_Detection**：乱码检测——判定文本是否出现编码损坏（异常码点比例、替换符、GBK↔latin1 特征等）。
- **Citation_Closure**：引用闭合——正文出现的每个 `[n]` 都有对应已验证文献；参考文献表只列被正文引用过的文献。
- **Bounded_Self_Heal**：有界自愈——对"存在可行修正路径"的问题（工具失败换法重试、护栏拒绝按原因改写重提）在有限次数内自动修正。
- **Review_Capability**：按需 LLM 评审能力（只读，产出评审报告，不自动改稿）；可实现为工具或独立评审子智能体。
- **Sub_Agent**：子智能体——带独立上下文的有界 agent 循环，作为"恰好是 agent 的工具"被顶层选择性委派。
- **Isolation_Beneficial_Task**：隔离即优点的子任务——独立评审、并行独立查询等彼此无需共享上下文的任务。
- **Curated_Context**：精选上下文——从共享工作区抽取的相关切片（全局摘要、术语表、邻居章节摘要、目标章节全文、相关文献），而非原始全量堆砌。
- **Shared_Workspace**：共享工作区（`PaperWorkspace`），一切工作单元的唯一真相源。

## Requirements

### Requirement 1: 输出验收（确定性核对可测需求）

**User Story:** 作为论文作者，我希望系统在交付前对照我的可测要求核对产出，以便坏结果一定被发现而不是静默交给我。

#### Acceptance Criteria

1. WHEN Top_Agent 判定一个任务即将完成，THE 系统 SHALL 运行与该任务相关的 Acceptance_Check 集合再交付结果。
2. WHERE 用户要求了具体输出格式（如 docx 两端对齐/行距/首行缩进），THE Acceptance_Check SHALL 校验这些排版规格是否已实际应用于产出文件。
3. WHERE 产出为 docx，THE Acceptance_Check SHALL 运行 Mojibake_Detection，判定正文是否存在编码损坏。
4. WHERE 用户要求的可测约束（如参考文献数量、年限范围）可从产出中判定，THE Acceptance_Check SHALL 核对产出是否满足这些约束。
5. THE Acceptance_Check SHALL 以不依赖 LLM 的确定性方式判定上述可测项。

### Requirement 2: 引用闭合

**User Story:** 作为论文作者，我希望参考文献表只包含正文真正引用过的文献，以便不出现"列了很多但没用"的冗余。

#### Acceptance Criteria

1. WHEN 系统导出论文，THE 系统 SHALL 使参考文献表只包含正文中被引用（出现对应 `[n]`）的文献。
2. WHERE 正文出现的某 `[n]` 在已验证文献库中无对应条目（悬空引用），THE Acceptance_Check SHALL 将其作为问题报告。
3. WHERE 已验证文献库中的某文献从未在正文被引用，THE 系统 SHALL 不将其列入导出的参考文献表。
4. THE Citation_Closure 检查 SHALL 以确定性方式进行，不依赖 LLM。

### Requirement 3: 诚实上报与有界自愈

**User Story:** 作为论文作者，我希望系统修不好时如实告诉我哪里不对，而不是假装完成或无限重试，以便我能信任它的交付状态。

#### Acceptance Criteria

1. IF Acceptance_Check 发现问题且存在可行修正路径（如工具可换法重试、内容可按护栏原因改写重提），THEN THE 系统 SHALL 在 Bounded_Self_Heal 的有限次数内尝试自动修正并重新验收。
2. THE Bounded_Self_Heal SHALL 受最大尝试次数与既有 token/时间预算约束。
3. IF 问题无可行修正路径（如环境缺失依赖、编码/代码类失败），THEN THE 系统 SHALL 停止重试并向用户明确上报问题与原因，而非静默交付或继续重试。
4. WHEN 交付最终结果，THE 系统 SHALL 报告哪些可测需求已满足、哪些未满足及原因。
5. WHILE 执行 Bounded_Self_Heal 的修正，THE 系统 SHALL 使所有修正经既有护栏与单一写路径，且不做破坏性的擅自改动（如篡改参考文献作者名）。

### Requirement 4: 按需 LLM 评审

**User Story:** 作为论文作者，我希望在需要时让系统认真评审我的论文质量，但平时做定点编辑时不被它打扰。

#### Acceptance Criteria

1. THE 系统 SHALL 提供一个只读的 Review_Capability，产出各维度评审意见与具体问题、改进建议，且不自动修改稿件。
2. WHEN 用户请求评审（如"帮我审逻辑/提改进意见"），THE Top_Agent SHALL 调用 Review_Capability 并把评审报告返回用户。
3. WHERE 任务为定点编辑（如设置排版、增补引用）且用户未请求评审，THE 系统 SHALL 不触发 Review_Capability。
4. WHERE Review_Capability 以独立子智能体实现，THE 子智能体 SHALL 以独立上下文进行评审（作为独立评审者，打破自评偏置）。

### Requirement 5: 选择性子智能体（混合架构）

**User Story:** 作为系统维护者，我希望只在收益最大处引入子智能体，以便获得上下文隔离与并行的好处而不增加无谓的复杂度与成本。

#### Acceptance Criteria

1. THE Top_Agent SHALL 对简单/局部操作（如设置排版、单章节编辑、导出）直接调用工具，而不 fork 子智能体。
2. WHERE 子任务属于 Isolation_Beneficial_Task（独立评审、并行独立查询），THE Top_Agent MAY 委派 Sub_Agent 执行。
3. THE 系统 SHALL 不对所有需求强制施加固定的"写→审"子智能体流程。
4. WHERE 多个彼此独立的子任务可并行（如批量核验文献），THE 系统 SHALL 支持其并行执行。
5. WHERE 委派了 Sub_Agent，THE Sub_Agent 对工作区的任何改动 SHALL 经与直接工具相同的护栏与单一写路径。

### Requirement 6: 章节写作维持全局理解（不做纯隔离）

**User Story:** 作为论文作者，我希望系统写/改某一章节时仍理解全文，以便术语一致、逻辑连贯，而不是脱离全局闭门造车。

#### Acceptance Criteria

1. WHEN 系统写作或改写某一章节，THE 系统 SHALL 为该工作单元提供 Curated_Context（含论文全局摘要、术语表、相邻章节摘要与目标章节全文），而非仅目标章节的孤立内容。
2. THE 系统 SHALL 以 Shared_Workspace 作为唯一真相源，各工作单元读取其精选视图而非各自维护副本。
3. THE 系统 SHALL 不对章节写作强制施加"无全局上下文的纯隔离子智能体"。
4. WHERE 章节写作以工具或子智能体实现，其对工作区的改动 SHALL 经既有护栏与单一写路径。

### Requirement 7: 有界性与向后兼容

**User Story:** 作为系统维护者，我希望这些新增能力受资源上限约束且不破坏既有行为，以便可控地演进。

#### Acceptance Criteria

1. THE Acceptance_Check 与 Review_Capability SHALL 受既有 token 预算与墙钟时间约束。
2. WHERE 未触发自愈、评审或子智能体，THE 系统 SHALL 保持既有增量工具路径的行为不变。
3. THE 系统 SHALL 保持一切工作区写入经"更新意图 → 护栏 → 单一写路径"落盘。
4. THE 子智能体与并行执行 SHALL 不绕过既有护栏、单一写路径与有界性约束。
