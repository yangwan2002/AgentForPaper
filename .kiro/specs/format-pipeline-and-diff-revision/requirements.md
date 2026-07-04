# Requirements Document

需求文档：format-pipeline-and-diff-revision（差量修订 + Markdown 内容契约与 pandoc 导出管线）

## Introduction

本特性在现有学术论文写作多智能体系统（`src/paper_agent/`）之上引入两项共享同一「Markdown 内容契约」的相关改进，且严格沿用既有核心契约（依赖倒置、`Agent`/`AgentResult`/`WorkspaceMutation` 协议、`Workspace_Repository` 为唯一原子写入路径、断点续跑、优雅降级、事件/可观测性与用量统计）。

**Part A — 差量/补丁优先的增量修订**：在写作—评审反馈循环中，把修订流程从「整章重写」（`WritingAgent._revise_content`）优先改为「产出最小化局部补丁」（复用既有 `edit_section` 工具与 `SectionEdit` 数据模型：锚点 + 替换，模式为 `replace`/`insert_after`/`insert_before`）。目标是：可审阅的干净 diff、未触及文本的字节级保留（强化既有「局部编辑不外溢」属性）、降低 token 消耗。仅在确属大范围结构性改动，或补丁锚点无法唯一定位（命中 0 处或 >1 处）时，回退到整章重写。

**Part B — Markdown 内容契约 + pandoc 导出管线 + 确定性格式闸 + LLM 修复循环**：确立内容契约——`SectionDraft.content` 必须是「规范化 Markdown（受约束子集）」，写作智能体须按该契约产出（图、引用、数学公式约定明确）。导出时用真实转换器（pandoc）将规范化 Markdown 转为 LaTeX/docx，取代当前各导出器各自手写的字符串拼接与朴素转义（`latex.py` 盲目转义所有特殊字符破坏数学/LaTeX、`docx.py` 把整段内容塞进单个段落）；Markdown 导出仍直接渲染。新增确定性格式闸：通过实际运行工具链（pandoc 转换错误、可选 pdflatex 编译错误）作为格式正确性的唯一裁判（AI 不得充当格式正确性的裁判），与既有 `Quality_Gate` 互补。当格式闸报告错误时，进入有界 LLM 修复循环（复用 `run_tool_loop` 模式）：把具体工具错误 + 出错片段喂给 LLM 修复后再校验，并区分「可由修复循环修复」与「重试耗尽 → 带可诊断原因报告并优雅降级」。

> 待用户确认的关键决策（DEC-1）：pandoc 为外部依赖。当 pandoc 不可用时，系统应「回退到受限的手写渲染器（保留现有行为，标注降级）」，还是「以明确可执行的错误信息快速失败」？本文档默认采用**可配置策略**（默认回退到受限渲染器并标注降级；可经配置切换为快速失败），见 Requirement 8，请用户确认默认取值。

## Glossary

- **Writing_Agent**：写作智能体，初次生成与局部修订章节草稿。
- **Orchestrator**：编排器，驱动写作—评审反馈循环与导出阶段。
- **Section_Draft**：章节草稿（`SectionDraft`），`content` 为正文、`cited_reference_ids` 为引用。
- **Content_Contract**：内容契约，规定 `Section_Draft.content` 必须为 Normalized_Markdown。
- **Normalized_Markdown**：规范化 Markdown，本特性定义的受约束 Markdown 子集（见 Requirement 5 的允许构造清单）。
- **Section_Edit_Tool**：既有章节级精确编辑工具（`edit_section`），按锚点定位产出 `SectionEdit` 意图。
- **Section_Edit**：章节级精确编辑意图（`SectionEdit`），含 `section_id`、`anchor`、`replacement`、`mode`。
- **Patch_Mode**：补丁优先修订模式，优先经 Section_Edit_Tool 产出最小补丁而非整章重写。
- **Whole_Section_Regeneration**：整章重写回退路径（既有 `_revise_content`）。
- **Tool_Loop**：既有有界工具循环（`run_tool_loop`），修复循环复用其模式。
- **Markdown_Exporter**：Markdown 导出器，直接渲染规范化 Markdown。
- **Pandoc_Pipeline**：基于 pandoc 的导出管线，将 Normalized_Markdown 转为 LaTeX/docx。
- **pandoc**：外部文档转换器可执行程序。
- **pdflatex**：外部 LaTeX 编译器可执行程序（可选，用于 LaTeX 产物的编译校验）。
- **Format_Gate**：确定性格式闸，通过实际运行工具链（pandoc/pdflatex）裁定格式正确性。
- **Format_Repair_Loop**：格式修复循环，将工具错误与出错片段交给 LLM 修复后再校验。
- **Quality_Gate**：既有确定性质量闸（内容层面检查），与 Format_Gate 互补。
- **Workspace_Repository**：工作区仓储，唯一的原子落盘写入路径。
- **Export_Result**：导出结果（`ExportResult`），含输出格式与产出文件路径列表。
- **达标阈值**：评审某维度被判为通过所需的最低分数（既有概念）。

## Requirements

### Requirement 1: 补丁优先的增量修订

**User Story:** 作为系统维护者，我希望修订流程优先产出最小化局部补丁而非整章重写，以便获得可审阅的干净 diff 并降低 token 消耗。

#### Acceptance Criteria

1. WHEN 反馈循环对某已存在草稿的章节执行修订且修订目标为内容型局部修改（即仅改动章节正文文本、不涉及章节的新增/删除/层级调整），THE Writing_Agent SHALL 优先经 Section_Edit_Tool 产出 1 至 20 个 Section_Edit 补丁，而不调用 Whole_Section_Regeneration。
2. WHEN Writing_Agent 经 Patch_Mode 产出 Section_Edit，THE Section_Edit SHALL 仅包含定位锚点 `anchor`、替换/插入文本 `replacement` 与模式 `mode`（取值属于 `{replace, insert_after, insert_before}`），不包含整章全文。
3. IF Writing_Agent 产出的 Section_Edit 的 `mode` 取值不属于 `{replace, insert_after, insert_before}`，THEN THE Writing_Agent SHALL 拒绝应用该补丁、保持目标章节当前内容不变，并返回指示模式取值非法的错误。
4. WHEN 一轮修订针对单个章节产出多个 Section_Edit，THE Writing_Agent SHALL 依次在该章节当前内容上逐条应用各补丁，且仅当每条补丁的锚点在应用时刻的当前内容中唯一命中（命中次数 == 1）时方可应用该补丁。
5. IF 某条 Section_Edit 的锚点在应用时刻的当前内容中命中次数为 0 或大于 1，THEN THE Writing_Agent SHALL 跳过该补丁、保持本轮已成功应用的补丁结果不变，并返回指示锚点未唯一命中（含命中次数）的错误。
6. WHEN 修订完成对目标章节的补丁应用，THE Writing_Agent SHALL 仅经 `AgentResult.mutations` 通过 Workspace_Repository 落盘更新后的章节，不直接写工作区。
7. WHERE 配置启用补丁优先修订（默认启用），THE Writing_Agent SHALL 对内容型修订采用 Patch_Mode 作为首选路径。

### Requirement 2: 未触及文本的字节级保留

**User Story:** 作为论文作者，我希望补丁修订只改动被定位的片段，未被定位的文本保持原样，以便修订可追溯且不引入意外改动。

#### Acceptance Criteria

1. WHEN Writing_Agent 对某章节应用一组 Section_Edit 补丁，THE Writing_Agent SHALL 使该章节中未被任何成功应用的补丁锚点覆盖的字符序列在修订前后字节级完全相同（逐字节比较结果为相等）。
2. WHEN 一轮修订仅针对部分章节，THE Writing_Agent SHALL 使未被本轮选为修订目标的所有章节的 `content` 在修订前后字节级完全相同。
3. WHEN Writing_Agent 对同一章节应用一组包含 2 至多条 Section_Edit 的补丁，THE Writing_Agent SHALL 按补丁在该组中的给定顺序逐条应用，且每条补丁的锚点命中次数均以「该条补丁应用时刻、已纳入前序成功补丁结果的当前内容」为基准计算。
4. IF 某条 Section_Edit 的锚点在其应用时刻的当前内容中命中次数不等于 1（0 处或 2 处及以上），THEN THE Writing_Agent SHALL 跳过该条补丁、保持当前内容字节级不变，并在 logs 中追加一条记录，该记录包含可定位的补丁标识、锚点未唯一命中的原因标识及实际命中次数。
5. IF 某条 Section_Edit 的锚点片段与同组中任一已成功应用补丁所改动的字符区间存在重叠，THEN THE Writing_Agent SHALL 跳过该条补丁、保持当前内容字节级不变，并在 logs 中追加一条标识「锚点区间冲突已跳过」的记录。
6. WHEN 一组补丁中无任何一条成功应用（包括补丁组为空、或全部被跳过的情形），THE Writing_Agent SHALL 使目标章节 `content` 字节级保持不变且不产生该章节的更新 mutation。
7. WHEN `mode == replace` 应用于唯一命中的锚点，THE Writing_Agent SHALL 仅以 `replacement` 置换锚点片段；WHEN `mode == insert_after`，SHALL 在锚点后紧邻插入 `replacement`；WHEN `mode == insert_before`，SHALL 在锚点前紧邻插入 `replacement`；在上述每种 mode 下，除被置换或新增的字符外，章节内其余字符序列均保持字节级不变。
8. WHERE 某条成功应用补丁的 `replacement` 为空字符串，THE Writing_Agent SHALL 在 `mode == replace` 时删除锚点片段、在 `mode == insert_after` 或 `mode == insert_before` 时不改变章节 `content` 的字节序列。

### Requirement 3: 整章重写回退

**User Story:** 作为写作智能体，我希望在补丁不适用时回退到整章重写，以便处理大范围结构性改动并保证修订始终可完成。

#### Acceptance Criteria

1. IF 本轮针对某章节的全部 Section_Edit 补丁均因锚点未唯一命中（命中 0 处或 >1 处）而无法应用，THEN THE Writing_Agent SHALL 对该章节回退到 Whole_Section_Regeneration。
2. WHERE 修订目标被判定为结构型改动（在受影响章节范围内新增/删除章节，或补丁累计影响字符数占章节当前 `content` 字符数比例超过 `patch_size_limit`，该比例取值范围 0.0–1.0、默认 0.5），THE Writing_Agent SHALL 采用 Whole_Section_Regeneration 而非 Patch_Mode。
3. WHEN Writing_Agent 执行 Whole_Section_Regeneration，THE Writing_Agent SHALL 仅替换目标章节的 `content`，未涉及的其他章节 `content` 字节级保持不变，且仅经 `AgentResult.mutations` 通过 Workspace_Repository 写回。
4. WHEN Whole_Section_Regeneration 产出新内容，THE 新内容 SHALL 同样符合 Content_Contract（Normalized_Markdown）。
5. WHEN 反馈循环对任意修订目标完成处理，THE Writing_Agent SHALL 在有限步处理内达成「已应用补丁」「完成整章重写」「无可应用变更且不改动工作区」三者之一且仅其一（修订路径完备且终止）。
6. IF Whole_Section_Regeneration 的产物不符合 Content_Contract，THEN THE Writing_Agent SHALL 丢弃该输出、保持目标章节 `content` 字节级不变，并记录可诊断原因。
7. IF Whole_Section_Regeneration 因模型调用失败或超时而无法产出，THEN THE Writing_Agent SHALL 不改动工作区并记录失败原因。

### Requirement 4: 修订差量可观测

**User Story:** 作为系统操作者，我希望每轮修订的补丁与回退决策可观测，以便审阅 diff 并评估 token 收益。

#### Acceptance Criteria

1. WHEN Writing_Agent 经 Patch_Mode 应用至少一条补丁，THE Writing_Agent SHALL 经既有可观测性系统发出日志事件，载荷含被修订的 `section_id`、成功应用的补丁数量与被跳过的补丁数量。
2. WHEN Writing_Agent 因补丁不适用而回退到 Whole_Section_Regeneration，THE Writing_Agent SHALL 发出日志事件并标识回退原因，原因取值于枚举集合 `{锚点未唯一命中, 结构型改动, 超过补丁适用上限}`。
3. WHEN 一轮修订完成，THE Writing_Agent SHALL 通过既有可观测性系统记录本轮采用的修订路径，路径取值于 `{Patch_Mode, Whole_Section_Regeneration}`。
4. THE 可观测记录 SHALL 不打印 API 密钥、完整请求体或章节正文全文，所记录的正文片段长度上限为 2000 字符。
5. WHEN 一轮修订完成，THE Writing_Agent SHALL 经既有用量统计系统记录本轮 token 用量，以支持 Patch_Mode 与 Whole_Section_Regeneration 的用量比较。

### Requirement 5: Markdown 内容契约

**User Story:** 作为系统架构师，我希望章节正文统一为规范化 Markdown，以便所有导出器与格式闸共享单一、明确的内容来源。

#### Acceptance Criteria

1. THE Content_Contract SHALL 规定 `Section_Draft.content` 为 Normalized_Markdown，其允许的构造严格限于以下受约束子集，且不得包含该子集之外的任何构造：段落、ATX 标题（`#`–`######`）、有序/无序列表、强调（`*`/`_`）、行内代码与围栏代码块、行内数学（`$...$`）与块级数学（`$$...$$`）、图片/图表引用、表格、以及方括号文献引用标注（如 `[arxiv:1706.03762]`）。
2. WHEN Writing_Agent 经初次生成路径产出 `content`，THE Writing_Agent SHALL 使该 `content` 完全符合 Content_Contract 第 1 条所定义的受约束子集。
3. WHEN Writing_Agent 经修订路径产出 `content`，THE Writing_Agent SHALL 使该 `content` 完全符合 Content_Contract 第 1 条所定义的受约束子集。
4. THE Content_Contract SHALL 规定数学公式仅以 `$...$`（行内）与 `$$...$$`（块级）两种形式表达，且数学定界符内部的内容不施加任何 Markdown 转义。
5. THE Content_Contract SHALL 规定文献引用以方括号标注文献 id（如 `[arxiv:1706.03762]`）；IF 被标注的 id 不存在于工作区已验证文献库（与既有 Property 1 一致），THEN THE 规范化函数 SHALL 以可诊断错误标识该引用位置，并保留原始内容不予丢弃。
6. THE Content_Contract SHALL 规定图表以 Markdown 图片语法或显式图表占位引用 `figure_id` 表达；THE Content_Contract SHALL 规定每个被引用的 `figure_id` 必须能唯一对应到工作区中一条 `FigureRecord`。
7. WHEN 规范化函数处理任意符合契约的输入 `x`，THE 系统 SHALL 保证其输出满足字节级幂等性，即 `normalize(normalize(x))` 与 `normalize(x)` 的结果字节级完全相同。
8. THE Content_Contract SHALL 规定单个 `Section_Draft.content` 的最大长度为 1,000,000 个 Unicode 字符；IF 某 `content` 超过该上限，THEN THE 规范化函数 SHALL 以可诊断错误标识超限，并保留原始内容不予截断或丢弃。
9. IF 某 `Section_Draft.content` 含契约未允许的构造，THEN THE 规范化函数 SHALL 要么将其归一化为契约内的等价表示，要么以可诊断错误标识不合规位置（指明字符偏移或行列位置），且在任一分支下均不静默丢弃任何内容。

### Requirement 6: 基于 pandoc 的导出管线

**User Story:** 作为论文作者，我希望 LaTeX 与 docx 由真实转换器从规范化 Markdown 生成，以便数学公式与特殊字符被正确处理而非被破坏。

#### Acceptance Criteria

1. WHEN 输出格式为 LaTeX 或 docx 且 pandoc 可执行文件可被定位并成功返回版本信息，THE Pandoc_Pipeline SHALL 将每个章节的 Normalized_Markdown 经 pandoc 转换为目标格式，而非手写字符串拼接与逐字符转义。
2. IF pandoc 可执行文件无法定位或无法返回版本信息，THEN THE Pandoc_Pipeline SHALL 终止本次导出、不产出任何目标格式文件，并返回指示 pandoc 不可用的错误。
3. WHEN Pandoc_Pipeline 转换含行内或块级数学（`$...$` / `$$...$$`）的内容，THE Pandoc_Pipeline SHALL 在产物中保留与源等价的数学标记，使数学定界符与其内部符号不被替换为转义后的纯文本字符。
4. IF 某章节的 pandoc 转换以非零状态失败，THEN THE Pandoc_Pipeline SHALL 终止本次导出、不写出部分或损坏的目标文件，并返回包含失败章节标识的错误。
5. WHEN Pandoc_Pipeline 生成 LaTeX 产物，THE Pandoc_Pipeline SHALL 同时产出与既有行为一致的 `<id>.tex` 与 `<id>.bib`，且 `.bib` 仅含已验证文献库条目（与既有 Req 10.5 一致）。
6. WHEN Pandoc_Pipeline 生成 docx 产物，THE Pandoc_Pipeline SHALL 使章节标题、段落、列表、数学与图表说明各自映射为 docx 中对应的结构化元素，而非将其全部合并入单一段落。
7. WHEN 导出完成，THE Pandoc_Pipeline SHALL 返回 Export_Result，其 `files` 字段列出全部实际产出文件的路径，且所列每个路径在文件系统中均存在。
8. THE Pandoc_Pipeline SHALL 仅经既有导出器接口（`DocumentExporter` / `get_exporter` 工厂）接入，不改变 Orchestrator 调用导出的方式。

### Requirement 7: Markdown 直接导出

**User Story:** 作为论文作者，我希望 Markdown 导出保持直接渲染，以便快速预览且无需外部依赖。

#### Acceptance Criteria

1. WHEN 输出格式为 Markdown，THE Markdown_Exporter SHALL 直接渲染 Normalized_Markdown 而不调用 pandoc。
2. THE Markdown_Exporter SHALL 在不调用 pandoc/pdflatex 等任何外部可执行程序的前提下成功产出 `<id>.md`（UTF-8 编码），即使 PATH 中缺失这些工具仍成功产出且不标注降级。
3. THE Markdown_Exporter SHALL 经 Workspace_Repository 落盘 `<id>.md` 并在 Export_Result.files 列出该路径。
4. WHEN 渲染章节，THE Markdown_Exporter SHALL 保留章节标题层级、章节顺序与正文字节级一致。
5. WHEN 渲染图表说明与参考文献，THE Markdown_Exporter SHALL 保留引用编号取值与排列顺序一致。
6. WHEN content 含数学（`$...$` / `$$...$$`）、代码或方括号文献引用标注，THE Markdown_Exporter SHALL 原样保留这些标记不做转义或改写（与 Req 6.3 数学语义不被破坏对齐）。
7. WHEN 工作区章节集合为空，THE Markdown_Exporter SHALL 产出仅含结构骨架的 `<id>.md` 且不报错（边界可终止）。

### Requirement 8: pandoc 不可用时的优雅降级（待用户确认 DEC-1）

**User Story:** 作为系统操作者，我希望在 pandoc 未安装时系统行为明确且可配置，以便在不同部署环境下都能得到可预期的结果。

#### Acceptance Criteria

1. WHEN 触发 LaTeX 或 docx 导出，THE Pandoc_Pipeline SHALL 在 5 秒内探测 pandoc 是否可用（可执行文件存在、可执行且能返回版本号）；IF 探测在 5 秒内未完成或未能返回版本号，THEN THE Pandoc_Pipeline SHALL 将 pandoc 判定为不可用。
2. WHERE 配置的降级策略为 `fallback`（默认），IF pandoc 不可用，THEN THE Pandoc_Pipeline SHALL 回退到不依赖 pandoc 的内置手写渲染器产出目标格式，并在 Export_Result 与事件日志中以一致措辞标注「已降级：pandoc 不可用」。
3. WHERE 配置的降级策略为 `fallback`，IF pandoc 不可用且内置手写渲染器也无法产出目标格式，THEN THE Pandoc_Pipeline SHALL 以错误失败，错误须指明失败的目标格式与失败原因且长度为 1 至 500 个字符，并在 Export_Result 中标注该格式导出失败，同时保留已成功产出的其他格式不被回滚。
4. WHERE 配置的降级策略为 `fail_fast`，IF pandoc 不可用，THEN THE Pandoc_Pipeline SHALL 以明确可执行的错误信息失败，错误须包含安装 pandoc 的指引且长度为 1 至 500 个字符。
5. WHEN pandoc 不可用且策略为 `fallback`，THE Orchestrator SHALL 不中止管线，仍完成其余已配置导出格式的产出（优雅降级）。
6. THE 降级策略配置项 SHALL 取值于 `{fallback, fail_fast}`，缺省为 `fallback`；IF 配置值不在该取值集合内，THEN THE Pandoc_Pipeline SHALL 以长度为 1 至 500 个字符的错误信息拒绝该配置并指明允许的取值。

### Requirement 9: 确定性格式闸（工具链为唯一裁判）

**User Story:** 作为系统维护者，我希望格式正确性由实际运行的工具链裁定而非 AI 判断，以便格式校验结果客观可信。

#### Acceptance Criteria

1. WHEN LaTeX 或 docx 产物生成后，THE Format_Gate SHALL 通过实际运行 pandoc 转换来裁定该产物的格式正确性，并将 pandoc 的退出码与错误输出作为唯一判定依据。
2. WHERE 输出格式为 LaTeX 且 pdflatex 可用且配置启用编译校验，THE Format_Gate SHALL 额外运行 pdflatex 编译 `.tex` 产物，并将其退出码与错误输出纳入判定依据。
3. WHEN 工具链全部成功（pandoc 退出码为 0，且启用时 pdflatex 退出码为 0），THE Format_Gate SHALL 判定格式正确（`passed == true`）。
4. IF 工具链任一步骤以非 0 退出码失败，THEN THE Format_Gate SHALL 判定格式不正确（`passed == false`），并产出结构化报告，报告须包含失败工具名、退出码、不超过 2000 字符的错误消息片段，以及可定位的出错片段（每段不超过 500 字符、最多 10 段，含产物中的行号或字符偏移）。
5. THE Format_Gate SHALL 不调用任何 LLM 来判定格式正确性（AI 不得充当格式正确性的裁判）。
6. THE Format_Gate SHALL 与既有 Quality_Gate 相互独立并互补：Quality_Gate 裁定内容层面问题，Format_Gate 裁定格式/可编译性问题。
7. WHEN Format_Gate 运行外部工具，THE Format_Gate SHALL 施加配置的超时上限（取值范围 1 至 600 秒，默认 60 秒），并在超时时判定格式不正确（`passed == false`）、终止该工具进程，并在结构化报告中记录超时工具名与所用超时阈值作为超时原因。
8. IF 裁定所需的工具（pandoc，或在启用 LaTeX 编译校验时的 pdflatex）在系统中不可用或不可执行，THEN THE Format_Gate SHALL 判定格式不正确（`passed == false`），并在结构化报告中记录缺失工具名与不可用原因，且不得调用任何 LLM 作为替代裁判。
9. WHEN Format_Gate 判定不通过（`passed == false`），THE Format_Gate SHALL 保留原始产物文件不被修改或删除，以便后续重试与诊断。

### Requirement 10: LLM 格式修复循环（有界）

**User Story:** 作为论文作者，我希望格式错误能被自动修复，以便减少人工干预并提高一次导出成功率。

#### Acceptance Criteria

1. WHEN Format_Gate 判定格式不正确，THE Format_Repair_Loop SHALL 将不超过 2000 字符的工具错误消息片段与经防御式截断后不超过 8000 字符的出错 Markdown 片段交给 LLM 以产出修复后的 Normalized_Markdown，并复用既有 Tool_Loop 模式。
2. WHEN Format_Repair_Loop 获得 LLM 的修复结果，THE Format_Repair_Loop SHALL 重新运行 Format_Gate 校验修复后的产物。
3. THE Format_Repair_Loop SHALL 受最大修复尝试次数 `max_repair_attempts` 约束（取值范围 0 至 10，默认 3），使工具链运行总次数不超过 `max_repair_attempts + 1`（保证终止性）。
4. WHEN 某次修复后 Format_Gate 判定 `passed == true`，THE Format_Repair_Loop SHALL 在该次尝试结束后即终止、不再发起后续修复尝试，并采用该次修复结果作为最终产物。
5. WHEN Format_Repair_Loop 应用 LLM 修复结果，THE Format_Repair_Loop SHALL 仅经 `AgentResult.mutations` 通过 Workspace_Repository 写回该次错误片段所对应章节的 `content`，不直接写工作区。
6. IF LLM 修复输出无法解析或不符合 Content_Contract，THEN THE Format_Repair_Loop SHALL 丢弃该次输出、不写回工作区、保持目标章节 `content` 字节级不变，并将该次计入尝试次数。
7. IF 某次修复的 LLM 调用失败（返回错误、超时或无有效响应），THEN THE Format_Repair_Loop SHALL 丢弃该次结果、不写回工作区、保持目标章节 `content` 字节级不变，并将该次计入尝试次数。
8. THE Format_Repair_Loop SHALL 把每次修复尝试的工具错误类别与尝试序号（取值 1 至 `max_repair_attempts`）通过既有可观测性系统记录，且不打印 API 密钥或完整请求体。

### Requirement 11: 修复耗尽与优雅降级

**User Story:** 作为系统操作者，我希望修复无法成功时系统带可诊断原因继续产出，以便始终获得尽可能可用的导出结果。

#### Acceptance Criteria

1. IF Format_Repair_Loop 达到 `max_repair_attempts`（取值范围 0–10，默认 3）仍未通过 Format_Gate，THEN THE Format_Repair_Loop SHALL 以结构化终止状态终止，状态取值于枚举 `{repaired_within_limit, repair_exhausted}`。
2. WHEN 修复重试耗尽未修复（`repair_exhausted`），THE Orchestrator SHALL 不中止管线，仍输出最近一次生成的产物（`max_repair_attempts == 0` 时为原始产物）。
3. WHEN 修复重试耗尽未修复，THE 系统 SHALL 在 Export_Result 与事件日志中标注「格式未通过：已达修复上限」及最后一次工具错误片段（不超过 2000 字符）。
4. WHEN 修复重试耗尽未修复，THE 系统 SHALL 保留可定位的诊断信息（失败工具名、退出码、出错片段，每字段不超过 2000 字符），经既有可观测性系统持久化供用户人工修复。
5. WHEN 修复循环以任意状态终止，THE 系统 SHALL 使工作区保留最后一次成功写回的章节 `content`、字节级不变且不回滚，使产物与工作区一致。
6. WHEN 修复循环以任意原因终止，THE Format_Repair_Loop SHALL 在 `max_repair_attempts + 1` 次工具链运行内完成（保证终止性）。

### Requirement 12: 契约保持与持久化原子性

**User Story:** 作为系统架构师，我希望本特性的所有新增组件遵循既有契约，以便保持依赖倒置、原子落盘、断点续跑与单一写入路径。

#### Acceptance Criteria

1. THE 本特性新增的所有工具与智能体逻辑 SHALL 仅经 `AgentResult.mutations` 通过 Workspace_Repository 落盘（100% 写入经此路径），不调用任何绕过 Workspace_Repository 的文件写入接口。
2. THE Pandoc_Pipeline、Format_Gate 与 Format_Repair_Loop SHALL 依赖抽象接口（导出器协议、LLM_Provider、工具注册表），不在 Orchestrator 业务代码中实例化具体实现类。
3. WHEN 管线中断后重启，THE Workspace_Repository SHALL 从最近一次成功提交状态恢复，使已完成章节 `content` 逐字节比对不变且不重复落盘（断点续跑）。
4. IF 重启时最近一次成功提交状态损坏不可恢复，THEN THE Workspace_Repository SHALL 以可诊断错误报告无法恢复，且不产生部分/损坏的工作区状态。
5. WHEN 落盘失败，THE Workspace_Repository SHALL 回滚并恢复到写入前的字节级状态，不留部分写入中间产物。
6. IF 落盘失败后回滚自身亦失败，THEN THE Workspace_Repository SHALL 以可诊断错误报告不一致风险，不静默继续。
7. WHEN Pandoc_Pipeline、Format_Gate 或 Format_Repair_Loop 调用 LLM 或外部工具，THE 系统 SHALL 经既有可观测性与用量统计系统记录事件与用量。
8. IF Format_Gate 或 Format_Repair_Loop 处理的外部工具输出或 LLM 输出超过 8000 字符，THEN THE 系统 SHALL 做防御式截断（保留不超过 8000 字符）后再解析。
9. THE 系统 SHALL 将外部工具输出与 LLM 输出视为不可信数据，不执行 `eval`/`exec`。
