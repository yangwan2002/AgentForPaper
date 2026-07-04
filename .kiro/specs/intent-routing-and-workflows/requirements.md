# Requirements Document

需求文档：intent-routing-and-workflows（意图路由 + 确定性工作流 + 自由智能体兜底）

## Introduction

当前所有任务——无论是"把 .tex 转成 docx"这种**流程固定**的任务，还是"把方法章节写得更有
说服力"这种**开放式**任务——都统一交给顶层自由智能体循环（`TaskAgent`）去自主编排工具。
这带来两个反复出现的问题：

1. **太依赖顶层模型智能**：固定任务的"用哪个工具、什么顺序、填什么参数"全靠模型当场想，
   模型稍弱就选错路径（如转格式走了 import+重建导致公式丢失）、越界干没被要求的事。
2. **执行不稳定**：同一个固定任务每次可能走不同路径，正确性靠模型自觉而非机制保证。

本特性引入一个**分层执行架构**，把"固定流程"从自由智能体手里拿走：

- **意图路由层**：用确定性信号（文件后缀 + 关键词）为主识别任务意图；模糊时问用户、不硬猜；
  执行前回显确认，给用户一次纠正机会。
- **确定性工作流层**：命中固定任务类型（转格式 / 保结构润色）时，走**步骤写死**的工作流，
  工具选择/顺序/参数由代码决定，不由顶层模型决定——从而**大幅降低对模型智能的依赖**。
- **自由智能体兜底层**：仅开放式任务（自由写作 / 复杂修改）才落到既有 `TaskAgent`，并保留
  既有护栏（单一写路径 / 权限 / 交付即停）。

设计约束（全程遵守）：**加法式、非侵入、失败诚实上报、默认不改变既有行为、误判可恢复且无损
原稿**。路由的"意图识别"只做单选式判断且层层加保险，绝不做开放式臆测。

## Glossary

- **意图（Intent）**：对用户单条请求的任务类型判定，取值为有限枚举（如 `convert_format` /
  `inplace_polish` / `open`）。
- **路由（Router）**：把用户请求映射到意图的组件；确定性信号优先，LLM 仅兜底。
- **置信度（Confidence）**：路由判定的可信程度；低于阈值触发澄清。
- **固定任务（Fixed Task）**：流程可写死为确定步骤序列的任务（转格式、保结构润色）。
- **工作流（Workflow）**：固定任务对应的确定性执行单元，步骤/工具/参数由代码决定、不经模型编排。
- **开放任务（Open Task）**：无固定流程、需模型智能的任务（自由写作、复杂修改），落自由智能体。
- **回显确认（Echo Confirmation）**：执行前用一句话复述判定的意图，供用户确认/纠正。

## Requirements

### Requirement 1: 意图路由（确定性信号优先）

**User Story:** 作为用户，我希望系统能准确判断我这条请求是"转格式"还是"润色"还是别的，且
主要靠可靠的规则而非模型猜测。

#### Acceptance Criteria
1. WHEN 用户提交一条请求 THE SYSTEM SHALL 先用确定性信号（源文件扩展名 + 请求文本关键词）尝试判定意图，不调用 LLM。
2. WHERE 确定性信号足以判定唯一意图 THE SYSTEM SHALL 直接采用该意图，不进入 LLM 分类。
3. WHERE 确定性信号不足或冲突 THE SYSTEM SHALL 才使用 LLM 做兜底分类，并产出置信度。
4. THE SYSTEM SHALL 使意图取值限定在有限枚举内（固定任务类型 + `open`），不产出枚举外的自由文本意图。

### Requirement 2: 低置信度必问（不硬猜）

**User Story:** 作为用户，当系统拿不准我要干嘛时，我希望它问我一句，而不是猜错了就闷头执行。

#### Acceptance Criteria
1. WHEN 路由判定的置信度低于阈值 THE SYSTEM SHALL 通过 `ask_user` 让用户在候选意图中选择，而不是直接执行。
2. WHERE 确定性信号相互冲突（如既像转格式又像润色） THE SYSTEM SHALL 视为低置信并触发澄清。
3. WHILE 等待用户澄清 THE SYSTEM SHALL 不执行任何工作流或工具。

### Requirement 3: 执行前回显确认

**User Story:** 作为用户，在系统对我的文件动手前，我希望它先说一句"我理解你要做 X"，让我能拦下误判。

#### Acceptance Criteria
1. WHEN 路由命中一个固定任务且将要执行工作流 THE SYSTEM SHALL 先回显判定的意图与关键参数（源文件、目标格式等），供用户确认。
2. IF 用户否定回显的意图 THEN THE SYSTEM SHALL 不执行该工作流，转为澄清或按用户更正重新路由。
3. WHERE 配置为"信号高置信时免确认" THE SYSTEM SHALL 允许对高置信固定任务跳过回显（可配置，默认对固定任务回显）。

### Requirement 4: 固定任务走确定性工作流

**User Story:** 作为用户，"把 .tex 转成 docx"这种固定流程，我希望每次都稳定走同一条正确路径，
而不是靠模型即兴发挥。

#### Acceptance Criteria
1. WHEN 意图命中固定任务类型（转格式 / 保结构润色） THE SYSTEM SHALL 执行对应的确定性工作流，而非把任务交给自由智能体循环。
2. THE SYSTEM SHALL 使工作流内的工具选择、调用顺序与参数由代码决定，顶层 LLM 不参与工具编排。
3. WHERE 工作流需要少量参数（如目标格式、是否双栏） THE SYSTEM SHALL 从确定性信号或用户确认中获取，而非让 LLM 自由决定。

### Requirement 5: 工作流的确定性、诚实与可恢复

**User Story:** 作为用户，即使系统偶尔判错任务，我也不希望我的原稿被破坏或丢失。

#### Acceptance Criteria
1. THE SYSTEM SHALL 使工作流产物写入新文件，绝不覆盖用户原始输入文件。
2. WHEN 工作流某一步失败（如缺 pandoc、转换报错） THE SYSTEM SHALL 如实上报失败原因，不静默降级为破坏性的重建路径。
3. THE SYSTEM SHALL 使同一输入与配置下工作流的执行步骤序列确定、可复现（不依赖 LLM 随机性）。
4. WHERE 一切写工作区的步骤存在 THE SYSTEM SHALL 仍经既有护栏与单一写路径（不绕过反幻觉/引用真实性）。

### Requirement 6: 开放任务落自由智能体（保留既有 harness）

**User Story:** 作为用户，开放式写作这类真正需要智能的任务，我仍希望交给能自由编排的智能体，
但它别乱来。

#### Acceptance Criteria
1. WHERE 意图为 `open`（无固定流程） THE SYSTEM SHALL 落到既有 `TaskAgent` 自由循环执行。
2. THE SYSTEM SHALL 在自由智能体分支保留既有约束：单一写路径 + 护栏、有界性、交付即停。
3. THE SYSTEM SHALL 不因引入路由而改变自由智能体分支的既有行为。

### Requirement 7: 向后兼容与加法式接入

**User Story:** 作为维护者，我要确保引入路由层不破坏现有功能，且能一键回退。

#### Acceptance Criteria
1. WHERE 路由未启用 THE SYSTEM SHALL 使所有请求走既有自由智能体路径，行为与现状逐字节一致。
2. WHERE 路由启用但未命中任何固定任务 THE SYSTEM SHALL 回退到自由智能体（永不因路由失败而拒绝服务）。
3. THE SYSTEM SHALL 使路由/工作流为独立可测组件，不侵入既有 `TaskAgent` 循环核心逻辑。
