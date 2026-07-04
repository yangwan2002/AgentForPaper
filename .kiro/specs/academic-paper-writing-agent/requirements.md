# Requirements Document

## Introduction

本功能旨在构建一个面向学术论文写作的多智能体（Multi-Agent）协作系统。系统借鉴通用智能体架构在上下文管理、记忆管理与工具管理方面的设计理念，通过多个职责明确的智能体协同完成论文写作任务。

系统支持两种输入模式：用户提供已有论文初稿进行修订润色，或用户提供主题背景与实验数据/图表由系统从零生成论文。核心智能体包括：规划智能体（Plan_Agent）负责拆解任务并生成任务清单；文献查询智能体（Paper_Query_Agent）与检索智能体（Search_Agent）负责收集背景文献；写作智能体（Writing_Agent）负责分章节撰写；评审反馈智能体（Review_Agent）负责评分与修订建议。系统通过一个持久化的共享论文工作区（Paper_Workspace）维护大纲、术语表、已验证参考文献库、各章节草稿与评审记录。

本系统的关键约束在于引用真实性：所有引用必须来自经过验证的真实文献，禁止伪造引用。

## Glossary

- **系统（System）**：本学术论文写作多智能体系统的整体。
- **协调器（Orchestrator）**：负责接收用户请求、调度各智能体、管理整体工作流的组件。
- **规划智能体（Plan_Agent）**：分析主题背景并产出任务清单（Task_Checklist）的智能体。
- **文献查询智能体（Paper_Query_Agent）**：针对已有文献库或知识源进行结构化查询的智能体。
- **检索智能体（Search_Agent）**：通过外部学术检索源获取候选文献并返回可核验元数据的智能体。
- **写作智能体（Writing_Agent）**：基于大纲与已收集信息分章节撰写论文内容的智能体。
- **评审智能体（Review_Agent）**：对草稿进行多维度评分并产出修订建议的智能体。
- **论文工作区（Paper_Workspace）**：持久化保存论文写作过程中所有共享状态的存储区。
- **任务清单（Task_Checklist）**：由规划智能体产出的、描述写作各步骤及其状态的结构化列表。
- **已验证参考文献库（Verified_Reference_Library）**：保存所有通过真实性核验文献条目的集合，存于论文工作区。
- **文献条目（Reference_Entry）**：单条文献记录，至少包含标题、作者、发表年份、来源标识符（如 DOI）。
- **真实性核验（Authenticity_Verification）**：确认某文献条目对应真实存在文献的检查过程。
- **输入模式（Input_Mode）**：用户发起请求时的两种方式之一，即"草稿修订模式"或"从零生成模式"。
- **草稿修订模式（Draft_Revision_Mode）**：用户提供已有论文初稿，由系统进行修订与润色的输入模式。
- **从零生成模式（Generation_Mode）**：用户提供主题背景与实验数据/图表，由系统从零生成论文的输入模式。
- **章节摘要（Section_Summary）**：对已完成章节内容的浓缩描述，用于在后续写作中提供全局上下文。
- **定位式局部修改（Localized_Edit）**：对已有章节草稿仅修改需要变更的局部内容、保留其余内容不变的修改方式，区别于整篇重新生成。
- **评分维度（Scoring_Dimension）**：评审智能体使用的评价维度，包括逻辑性、新颖性、论证充分性与语言质量。
- **质量阈值（Quality_Threshold）**：判定草稿是否达到可接受质量的评分下限。
- **迭代上限（Iteration_Limit）**：写作—评审反馈循环允许执行的最大轮数。
- **图表数据（Figure_Data）**：用户提供的实验数据、图像或图表文件。
- **图表说明（Figure_Caption）**：对某一图表的文字描述。
- **引用审计（Citation_Audit）**：在草稿修订模式下，对用户初稿中的参考文献与正文引用进行的核验过程，包含存在性、元数据准确性与引用-文献对应性检查。
- **输出格式（Output_Format）**：用户为最终论文选择的导出格式，取值为 LaTeX 或 docx。
- **文档导出器（Document_Exporter）**：将论文工作区中的论文内容渲染为指定输出格式（Output_Format）文件的组件。

## Requirements

### Requirement 1: 接收用户请求并识别输入模式

**User Story:** 作为研究者，我希望系统能够识别我提供的是已有论文初稿还是主题背景加实验数据，以便系统选择正确的写作工作流。

#### Acceptance Criteria

1. WHEN 用户发起论文写作请求且请求包含已有论文初稿，THE 协调器 SHALL 将本次请求标记为草稿修订模式（Draft_Revision_Mode）
2. WHEN 用户发起论文写作请求且请求包含主题背景与图表数据（Figure_Data）而不含已有论文初稿，THE 协调器 SHALL 将本次请求标记为从零生成模式（Generation_Mode）
3. IF 用户请求既不包含已有论文初稿也不包含主题背景，THEN THE 协调器 SHALL 返回提示信息并请求用户补充必要输入
4. WHEN 输入模式被确定后，THE 协调器 SHALL 在论文工作区（Paper_Workspace）中记录本次请求的输入模式（Input_Mode）

### Requirement 2: 规划智能体生成任务清单

**User Story:** 作为研究者，我希望系统先分析主题背景并制定写作计划，以便整个写作过程有清晰的步骤可循。

#### Acceptance Criteria

1. WHEN 协调器完成输入模式识别，THE 规划智能体（Plan_Agent）SHALL 基于主题背景生成任务清单（Task_Checklist）
2. THE 任务清单（Task_Checklist）SHALL 包含论文大纲及每个章节对应的写作任务条目
3. THE 规划智能体（Plan_Agent）SHALL 将生成的任务清单（Task_Checklist）写入论文工作区（Paper_Workspace）
4. WHERE 输入模式为草稿修订模式（Draft_Revision_Mode），THE 规划智能体（Plan_Agent）SHALL 基于已有论文初稿的结构生成任务清单（Task_Checklist）
5. IF 规划智能体判定当前背景信息不足以生成完整大纲，THEN THE 规划智能体（Plan_Agent）SHALL 在任务清单（Task_Checklist）中标记需要进行文献检索的任务条目

### Requirement 3: 文献检索与背景信息收集

**User Story:** 作为研究者，我希望系统在背景信息不足时自动检索相关文献，以便论文有充分的学术背景支撑。

#### Acceptance Criteria

1. WHEN 任务清单（Task_Checklist）中存在标记为需要文献检索的任务条目，THE 检索智能体（Search_Agent）SHALL 通过外部学术检索源获取候选文献条目（Reference_Entry）
2. THE 文献查询智能体（Paper_Query_Agent）SHALL 针对论文工作区（Paper_Workspace）中已有的文献库执行结构化查询并返回匹配的文献条目（Reference_Entry）
3. THE 检索智能体（Search_Agent）SHALL 为每条候选文献条目（Reference_Entry）返回标题、作者、发表年份与来源标识符
4. WHEN 文献检索完成，THE 检索智能体（Search_Agent）SHALL 将候选文献条目（Reference_Entry）提交进行真实性核验（Authenticity_Verification）

### Requirement 4: 引用真实性核验（硬约束）

**User Story:** 作为研究者，我希望系统引用的每一篇文献都真实存在且可核验，以便论文不包含任何伪造引用。

#### Acceptance Criteria

1. WHEN 一条文献条目（Reference_Entry）通过真实性核验（Authenticity_Verification），THE 系统 SHALL 将该文献条目存入已验证参考文献库（Verified_Reference_Library）
2. IF 一条文献条目（Reference_Entry）未通过真实性核验（Authenticity_Verification），THEN THE 系统 SHALL 拒绝将该文献条目存入已验证参考文献库（Verified_Reference_Library）
3. THE 写作智能体（Writing_Agent）SHALL 仅引用已验证参考文献库（Verified_Reference_Library）中的文献条目（Reference_Entry）
4. IF 写作智能体在撰写时需要引用某文献而该文献不在已验证参考文献库（Verified_Reference_Library）中，THEN THE 写作智能体（Writing_Agent）SHALL 触发针对该文献的真实性核验（Authenticity_Verification）流程而不直接生成引用
5. THE 真实性核验（Authenticity_Verification）SHALL 通过比对来源标识符与外部学术检索源确认文献条目对应真实存在的文献

### Requirement 5: 写作智能体分章节撰写

**User Story:** 作为研究者，我希望系统逐章节撰写论文并保持全局一致性，以便降低幻觉与上下文溢出风险。

#### Acceptance Criteria

1. THE 写作智能体（Writing_Agent）SHALL 依据论文工作区（Paper_Workspace）中的大纲逐个章节撰写内容
2. WHEN 写作智能体完成一个章节的撰写，THE 写作智能体（Writing_Agent）SHALL 为该章节生成章节摘要（Section_Summary）并写入论文工作区（Paper_Workspace）
3. WHEN 写作智能体撰写某一章节，THE 写作智能体（Writing_Agent）SHALL 使用论文工作区（Paper_Workspace）中的大纲与已完成章节的章节摘要（Section_Summary）作为全局上下文
4. WHERE 输入模式为草稿修订模式（Draft_Revision_Mode），THE 写作智能体（Writing_Agent）SHALL 基于用户提供的已有论文初稿对应章节进行修订而非从零撰写
5. THE 写作智能体（Writing_Agent）SHALL 使用论文工作区（Paper_Workspace）中的术语表保持术语使用的一致性
6. WHEN 写作智能体完成一个章节的撰写，THE 写作智能体（Writing_Agent）SHALL 将该章节草稿写入论文工作区（Paper_Workspace）
7. WHERE 论文工作区（Paper_Workspace）中已存在某章节的草稿，THE 写作智能体（Writing_Agent）SHALL 对该章节执行定位式的局部修改（Localized_Edit）而非整篇重新生成
8. WHEN 修订建议为针对具体内容的修改，THE 写作智能体（Writing_Agent）SHALL 仅修改修订建议所指向的局部内容并保留草稿中未被建议涉及的其余内容
9. WHERE 修订建议为结构性调整（如章节重组、新增或删除章节），THE 写作智能体（Writing_Agent）SHALL 在受影响章节范围内执行重写、新增或删除而不改动未受影响的章节

### Requirement 6: 图表与实验数据处理

**User Story:** 作为研究者，我希望系统能够处理我提供的实验数据与图表，以便论文中包含正确的图表说明。

#### Acceptance Criteria

1. WHEN 用户提供的图表数据（Figure_Data）包含图表说明（Figure_Caption），THE 写作智能体（Writing_Agent）SHALL 使用用户提供的图表说明（Figure_Caption）
2. WHERE 用户提供的图表数据（Figure_Data）不含图表说明（Figure_Caption），THE 写作智能体（Writing_Agent）SHALL 基于图表数据（Figure_Data）生成图表说明（Figure_Caption）
3. THE 系统 SHALL 将图表数据（Figure_Data）与其对应的图表说明（Figure_Caption）存入论文工作区（Paper_Workspace）
4. WHEN 写作智能体在正文中引用某一图表，THE 写作智能体（Writing_Agent）SHALL 引用论文工作区（Paper_Workspace）中已存在的图表数据（Figure_Data）

### Requirement 7: 评审反馈与评分

**User Story:** 作为研究者，我希望系统对草稿进行多维度评分并给出修订建议，以便论文质量得到客观评估和持续改进。

#### Acceptance Criteria

1. WHEN 写作智能体完成一轮草稿撰写，THE 评审智能体（Review_Agent）SHALL 针对逻辑性、新颖性、论证充分性与语言质量四个评分维度（Scoring_Dimension）对草稿评分
2. THE 评审智能体（Review_Agent）SHALL 为每个评分维度（Scoring_Dimension）产出对应的修订建议
3. THE 评审智能体（Review_Agent）SHALL 将评分结果与修订建议作为评审记录写入论文工作区（Paper_Workspace）
4. WHEN 评审智能体完成评分，THE 评审智能体（Review_Agent）SHALL 将评审记录反馈给写作智能体（Writing_Agent）

### Requirement 8: 反馈循环终止条件

**User Story:** 作为研究者，我希望写作—评审循环在质量达标或达到迭代上限时停止，以便避免无限循环并控制成本。

#### Acceptance Criteria

1. WHILE 草稿各评分维度（Scoring_Dimension）的得分均达到或超过质量阈值（Quality_Threshold），THE 系统 SHALL 终止写作—评审反馈循环
2. WHILE 写作—评审反馈循环的轮数达到迭代上限（Iteration_Limit），THE 系统 SHALL 终止写作—评审反馈循环
3. WHEN 写作—评审反馈循环终止，THE 系统 SHALL 向用户返回当前论文工作区（Paper_Workspace）中的最新论文草稿
4. IF 写作—评审反馈循环因达到迭代上限（Iteration_Limit）而终止且草稿未达到质量阈值（Quality_Threshold），THEN THE 系统 SHALL 在返回结果中标注未达标的评分维度（Scoring_Dimension）

### Requirement 9: 共享论文工作区与记忆管理

**User Story:** 作为研究者，我希望系统在一个持久化的共享工作区中维护写作过程中的所有状态，以便各智能体协作可靠且过程可追溯。

#### Acceptance Criteria

1. THE 论文工作区（Paper_Workspace）SHALL 持久化保存大纲、术语表、已验证参考文献库（Verified_Reference_Library）、各章节草稿、章节摘要（Section_Summary）与评审记录
2. WHEN 任一智能体更新论文工作区（Paper_Workspace）中的内容，THE 系统 SHALL 持久化保存更新后的内容
3. IF 论文工作区（Paper_Workspace）的持久化保存操作失败，THEN THE 系统 SHALL 阻止本次更新操作直至持久化保存成功
4. THE 系统 SHALL 允许各智能体读取论文工作区（Paper_Workspace）中的共享状态
5. WHEN 一次写作任务结束，THE 论文工作区（Paper_Workspace）SHALL 保留本次任务的完整记录以供后续查阅

### Requirement 10: 用户可选的输出格式

**User Story:** 作为研究者，我希望能够选择最终论文的导出格式（LaTeX 或 docx），以便满足不同期刊、会议或协作场景的提交要求。

#### Acceptance Criteria

1. WHEN 用户发起论文写作请求，THE 协调器 SHALL 允许用户指定输出格式（Output_Format）为 LaTeX 或 docx
2. IF 用户未指定输出格式（Output_Format），THEN THE 系统 SHALL 采用默认输出格式（Output_Format）
3. THE 协调器 SHALL 将用户选择的输出格式（Output_Format）记录到论文工作区（Paper_Workspace）
4. WHEN 写作—评审反馈循环终止，THE 文档导出器（Document_Exporter）SHALL 依据论文工作区（Paper_Workspace）中记录的输出格式（Output_Format）将论文内容渲染为对应格式的文件
5. WHERE 输出格式（Output_Format）为 LaTeX，THE 文档导出器（Document_Exporter）SHALL 同时导出包含已验证参考文献库（Verified_Reference_Library）条目的 BibTeX 文件
6. THE 文档导出器（Document_Exporter）SHALL 在导出文件中保留各章节草稿、图表说明（Figure_Caption）与对已验证参考文献库（Verified_Reference_Library）条目的引用

### Requirement 11: 草稿引用审计

**User Story:** 作为研究者，当我提供已有论文初稿时，我希望系统核验初稿中引用的参考文献是否真实、准确、且与正文一一对应，以便发现并修正伪造引用、错误元数据与悬空引用。

#### Acceptance Criteria

1. WHERE 输入模式为草稿修订模式（Draft_Revision_Mode），THE 系统 SHALL 在写作前对初稿执行引用审计（Citation_Audit）
2. THE 系统 SHALL 从初稿中解析出参考文献列表与正文内引用标记
3. WHEN 某条参考文献无法在外部学术来源中核验为真实存在，THE 系统 SHALL 在引用审计（Citation_Audit）结果中将其标记为疑似不存在
4. WHEN 某条参考文献的发表年份与真实记录不一致，THE 系统 SHALL 在引用审计（Citation_Audit）结果中标记元数据可能有误
5. WHEN 正文引用了某编号但参考文献列表中无对应条目，THE 系统 SHALL 在引用审计（Citation_Audit）结果中标记悬空引用
6. WHEN 参考文献列表中某条目从未在正文中被引用，THE 系统 SHALL 在引用审计（Citation_Audit）结果中标记冗余文献
7. WHEN 一条参考文献被核验为真实存在，THE 系统 SHALL 将其对应的真实记录写入已验证参考文献库（Verified_Reference_Library）
8. THE 系统 SHALL 将引用审计（Citation_Audit）结果持久化保存至论文工作区（Paper_Workspace）
