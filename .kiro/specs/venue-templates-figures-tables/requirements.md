# Requirements Document

需求文档：venue-templates-figures-tables（会议模板 + 真实图片嵌入 + 结果表生成 + 数据出图）

## Introduction

本特性在现有学术论文写作多智能体系统（`src/paper_agent/`）之上，补齐 SOTA 差距分析中识别出的四项「可发表性（publishability）」缺口，目标是让系统产出的论文更接近可直接投稿状态（用户仅需少量微调）。四项缺口为：

1. **会议/期刊模板（Venue Templates）**：当前 LaTeX 导出硬编码 `\documentclass{article}`（见 `export/latex.py`），无法产出符合特定投稿要求的文档类、样式文件与结构。需支持按投稿目标（如 NeurIPS、ICML、ACL 风格、IEEE）选择模板，包含模板选择方式（配置/档案）、样式资产的提供/引用方式，以及请求的模板不可用时的优雅回退。
2. **真实图片嵌入（Real Figure Embedding）**：当前 LaTeX 导出对图表仅产出 `\caption` 与 `\label`，既无 `\includegraphics` 也无真实图像文件（全库无 `includegraphics`）；docx 仅把图表说明塞入普通段落。需让图表被真实嵌入（LaTeX 经 `\includegraphics` 引用图像文件、docx 经内联图片嵌入），使其出现在编译产物中。
3. **结果表生成（Results Table Generation）**：当前系统完全没有表格生成（全库无 `tabular`/`table`）。系统已有结构化实验数据（`ResearchArtifact.experiments[].results_data.stats`，每指标含 mean/std/min/max，及 baselines/datasets），需把该真实实验数据渲染为规范的结果表（LaTeX `tabular` / docx 表格），且严格 grounded 于 artifact 数据（不得编造数字，与既有 `quality_gate` 的 artifact-grounding 检查一致）。
4. **数据出图（Figures-from-Data，P1）**：从 artifact 实验数据生成真实图表（而非当前 `writing_agent._process_figures` 仅生成文字说明），产出图像文件供缺口 2 嵌入。

本特性严格沿用本代码库既有契约：智能体从不直接写工作区，只返回 `AgentResult.mutations` 经 `Workspace_Repository` 原子应用（唯一写入路径）；依赖倒置（依赖 `DocumentExporter` 协议 / `get_exporter` 工厂、`LLMProvider`、检索等抽象接口，而非在编排器中实例化具体类）；优雅降级与断点续跑；对任何外部工具/LLM 调用做可观测性与用量统计；输出数字必须 grounded 于用户提供的 `ResearchArtifact`（不得编造）；外部工具输出与 LLM 输出视为不可信数据（禁用 `eval`/`exec`、做防御式截断）。当前支持的输出格式为 LaTeX、docx、Markdown。

### 范围边界（Out of Scope，明确不重复）

以下内容由既有 spec **`format-pipeline-and-diff-revision`** 拥有，本特性不重复实现，仅作为下游消费者/上游生产者与其衔接：

- 基于 pandoc 的 Markdown→LaTeX/docx 转换管线（`Pandoc_Pipeline`）。
- 确定性格式闸（`Format_Gate`，运行 pandoc + pdflatex 编译，作为格式正确性的唯一裁判）。
- 有界 LLM 格式修复循环（`Format_Repair_Loop`）。
- 规范化 Markdown 内容契约（`Normalized_Markdown` / `Content_Contract`）。

**衔接约定**：本特性负责「产出正确的模板脚手架、图像文件、`\includegraphics`/表格标记、以及嵌入图表所需的 Normalized_Markdown 构造与资产」，转换与编译校验仍由上述既有 spec 拥有的 `Pandoc_Pipeline` 与 `Format_Gate` 负责。本文档不定义 pandoc/pdflatex 的调用、格式裁定或修复逻辑。

## Glossary

- **Template_Engine**：会议模板引擎，本特性新增组件，负责按选定 `Venue_Profile` 产出目标格式的模板脚手架（文档类、样式资产引用、结构骨架）。
- **Venue_Profile**：会议档案，描述某投稿目标（如 NeurIPS/ICML/ACL/IEEE）所需的文档类名、样式资产清单、结构约束与元数据，由配置/论文档案选定。
- **Venue_Id**：会议档案标识符（如 `neurips`、`icml`、`acl`、`ieee`、`default`），用于选择 `Venue_Profile`。
- **Style_Asset**：样式资产，模板所需的 `.sty`/`.cls`/`.bst` 等文件或其可解析引用（本地路径或档案内置资产）。
- **Figure_Renderer**：数据出图渲染器，本特性新增组件，从 `Experiment.results_data` 生成图像文件（缺口 4）。
- **Table_Renderer**：结果表渲染器，本特性新增组件，从 `Experiment.results_data.stats` 生成 LaTeX `tabular` / docx 表格标记（缺口 3）。
- **Figure_Asset**：图像资产，`Figure_Renderer` 或用户提供的、被 `\includegraphics`/docx 内联图片引用的真实图像文件。
- **Research_Artifact**：用户提供的结构化真实研究内容（`ResearchArtifact`），含 `experiments[]`，每个 `Experiment` 有 `results_data`（`columns`/`rows`/`stats`，`stats` 每指标含 mean/std/min/max）、`dataset`、`baselines`、`metrics`。
- **Figure_Record**：图表记录（`FigureRecord`），含 `figure_id`、`data_ref`、`caption`、`caption_provided_by_user`。
- **Document_Exporter**：文档导出器抽象协议（`DocumentExporter` / `get_exporter` 工厂），本特性经此接口接入，不改变 Orchestrator 调用导出的方式。
- **Latex_Exporter**：LaTeX 导出器（`export/latex.py`）。
- **Docx_Exporter**：docx 导出器（`export/docx.py`）。
- **Writing_Agent**：写作智能体（`agents/writing_agent.py`），当前经 `_process_figures` 仅产出图表文字说明。
- **Quality_Gate**：既有确定性质量闸（`tools/quality_gate.py`），含 artifact-grounding 检查（正文/表格数字必须能在 artifact 数值集合中找到）。
- **Workspace_Repository**：工作区仓储，唯一的原子落盘写入路径（智能体只经 `AgentResult.mutations` 写回）。
- **Export_Result**：导出结果（`ExportResult`），含输出格式与产出文件路径列表 `files`。
- **Pandoc_Pipeline**：既有 spec `format-pipeline-and-diff-revision` 拥有的 pandoc 导出管线（本特性的下游转换器，不在本特性范围）。
- **Format_Gate**：既有 spec `format-pipeline-and-diff-revision` 拥有的确定性格式闸（本特性不实现）。
- **Grounded_Value**：可在 `Research_Artifact` 数值集合（含 `all_numeric_values()` 及 `stats` 的 mean/std/min/max 衍生值）中按既有质量闸容差找到的数值。

## Requirements

### Requirement 1: 会议模板选择

**User Story:** 作为论文作者，我希望按投稿目标选择会议模板，以便导出的论文使用该会议要求的文档类与结构，减少投稿前的手工改造。

#### Acceptance Criteria

1. WHERE 配置或论文档案指定了 `Venue_Id`，THE Template_Engine SHALL 选择与该 `Venue_Id` 对应的 `Venue_Profile` 作为本次导出所用模板。
2. WHERE 配置与论文档案均未指定 `Venue_Id`，THE Template_Engine SHALL 选择 `Venue_Id` 为 `default` 的 `Venue_Profile`（对应现行 `\documentclass{article}` 行为）。
3. WHEN 输出格式为 LaTeX 且已选定非 `default` 的 `Venue_Profile`，THE Latex_Exporter SHALL 使 `.tex` 产物的文档类声明为该 `Venue_Profile` 指定的文档类，而非硬编码的 `\documentclass{article}`。
4. WHEN 已选定某 `Venue_Profile`，THE Template_Engine SHALL 使产出的模板脚手架包含该 `Venue_Profile` 声明的必需结构元素（文档类、样式资产引用、标题/作者区、正文区）。
5. THE 系统 SHALL 支持至少以下 `Venue_Id` 取值：`neurips`、`icml`、`acl`、`ieee`、`default`；IF 配置提供的 `Venue_Id` 不属于系统已注册的取值集合，THEN THE Template_Engine SHALL 按 Requirement 3 的回退规则处理并记录该未注册取值。
6. WHEN 已选定某 `Venue_Profile` 且输出格式为 docx，THE Docx_Exporter SHALL 应用该 `Venue_Profile` 声明的 docx 结构约定（标题层级、章节结构），在 docx 不支持某 LaTeX 专属样式时按 Requirement 3 记录降级而不失败。
7. THE Template_Engine SHALL 经 Document_Exporter 抽象协议与既有导出器接入，不改变 Orchestrator 调用导出的方式，也不在 Orchestrator 业务代码中实例化具体导出器类。

### Requirement 2: 会议模板样式资产的提供与引用

**User Story:** 作为论文作者，我希望模板所需的样式文件被正确引用或提供，以便编译时能定位到样式并产出符合会议排版的文档。

#### Acceptance Criteria

1. WHEN 已选定某 `Venue_Profile`，THE Template_Engine SHALL 在 `.tex` 产物中生成该 `Venue_Profile` 所需 `Style_Asset` 的引用声明（如 `\usepackage`/`\documentclass` 选项）。
2. WHERE 某 `Venue_Profile` 附带内置 `Style_Asset` 文件，THE Template_Engine SHALL 经 `Workspace_Repository` 将这些 `Style_Asset` 文件落盘到导出目录，并使其路径出现在 `Export_Result.files` 中。
3. WHEN Template_Engine 落盘 `Style_Asset` 文件，THE 系统 SHALL 使 `.tex` 产物中的样式引用名与实际落盘的 `Style_Asset` 文件名一致。
4. IF 某 `Venue_Profile` 声明的 `Style_Asset` 既无内置文件也无可解析的引用来源，THEN THE Template_Engine SHALL 按 Requirement 3 的回退规则处理，并在报告中记录缺失的 `Style_Asset` 名称。
5. THE Template_Engine SHALL 将 `Venue_Profile` 与 `Style_Asset` 的来源内容视为不可信数据，不对其执行 `eval`/`exec`，并对写入 `.tex` 的样式引用名做长度上限为 500 字符的防御式截断。

### Requirement 3: 模板不可用时的优雅回退

**User Story:** 作为系统操作者，我希望在请求的模板不可用时系统仍能产出可用文档并明确标注降级，以便在不同部署环境下都获得可预期的结果。

#### Acceptance Criteria

1. IF 请求的 `Venue_Id` 满足以下任一不可用条件（`unregistered_venue`：`Venue_Id` 未在模板注册表中登记；`missing_style_asset`：其 `Venue_Profile` 所需的 `Style_Asset` 不存在或无法加载；`invalid_profile`：其 `Venue_Profile` 无法解析或校验失败），THEN THE Template_Engine SHALL 回退到 `default` 的 `Venue_Profile` 并产出与请求一致的目标格式文档，而非中止导出。
2. WHEN Template_Engine 发生模板回退，THE 系统 SHALL 在 `Export_Result` 与事件日志两处均写入逐字节相同的降级标注文本「已降级：请求的会议模板不可用，已回退到默认模板」，且该标注 SHALL 附带被请求的 `Venue_Id` 与取值于枚举 `{unregistered_venue, missing_style_asset, invalid_profile}` 的回退原因。
3. WHEN Template_Engine 发生模板回退，THE 系统 SHALL 产出内容完整的目标格式文档，其章节、图表、表格与参考文献的数量及内容 SHALL 与未回退时相同，仅模板样式回退到默认，不删除或截断任何上述内容单元。
4. WHEN Template_Engine 执行模板回退过程，THE 系统 SHALL 不调用任何 LLM 作为模板正确性的裁判。
5. WHEN 模板回退发生，THE 系统 SHALL 经既有可观测性系统记录恰好一条事件，其中含被请求的 `Venue_Id` 与取值于枚举 `{unregistered_venue, missing_style_asset, invalid_profile}` 的回退原因，且每次导出至多触发一次回退（回退目标固定为 `default`，不再向其他 `Venue_Profile` 级联）。
6. IF 回退目标 `default` 的 `Venue_Profile` 或其 `Style_Asset` 同样不可用，THEN THE Template_Engine SHALL 中止本次导出、不产出目标格式文档，并经既有可观测性系统记录一条指示"默认模板不可用"的错误事件，同时保持输入数据不被修改。

### Requirement 4: LaTeX 真实图片嵌入

**User Story:** 作为论文作者，我希望图表在编译后的 LaTeX 文档中真实显示图像，以便审稿人看到实际图片而非空的图题占位。

#### Acceptance Criteria

1. WHEN 输出格式为 LaTeX 且某 `Figure_Record` 存在可定位的 `Figure_Asset` 图像文件，THE Latex_Exporter SHALL 在该图的 `figure` 环境中生成引用该 `Figure_Asset` 的 `\includegraphics` 命令，且该命令位于 `\caption` 与 `\label` 之前。
2. WHEN Latex_Exporter 生成 `\includegraphics`，THE Latex_Exporter SHALL 使其引用的图像路径与实际落盘于导出目录的 `Figure_Asset` 文件路径一致，且该文件出现在 `Export_Result.files` 中。
3. WHEN 某 `Figure_Record` 被嵌入，THE Latex_Exporter SHALL 保留既有的 `\caption`（图题）与 `\label`（`figure_id`）输出行为不变。
4. IF 某 `Figure_Record` 无可定位的 `Figure_Asset` 图像文件，THEN THE Latex_Exporter SHALL 保留仅含 `\caption` 与 `\label` 的既有回退输出、不生成 `\includegraphics`，并记录该图缺失图像资产。
5. THE Latex_Exporter SHALL 将 `Figure_Asset` 文件路径视为不可信输入，仅生成指向导出目录内文件的相对路径引用，不写出指向导出目录之外的绝对路径。
6. WHEN 一次导出嵌入多个图，THE Latex_Exporter SHALL 使每个 `Figure_Record` 的 `\includegraphics` 引用其自身对应的 `Figure_Asset`，不发生图与资产的错配。

### Requirement 5: docx 真实图片嵌入

**User Story:** 作为使用 docx 协作的作者，我希望图表以内联图片嵌入 docx，以便在 Word 中直接看到图像。

#### Acceptance Criteria

1. WHEN 输出格式为 docx 且某 `Figure_Record` 存在可定位的 `Figure_Asset` 图像文件，THE Docx_Exporter SHALL 将该 `Figure_Asset` 作为内联图片嵌入 docx，并在图片下方输出对应的图题文本。
2. IF 某 `Figure_Record` 无可定位的 `Figure_Asset` 图像文件，THEN THE Docx_Exporter SHALL 保留仅输出 `figure_id` 与图题文本的既有回退行为，并记录该图缺失图像资产。
3. IF 处理 docx 图片嵌入所需的可选依赖不可用，THEN THE Docx_Exporter SHALL 以指明缺失依赖的可诊断错误处理，且不产出部分损坏的 docx 文件。
4. WHEN Docx_Exporter 嵌入图片，THE Export_Result.files SHALL 列出实际产出的 docx 文件路径，且该路径在文件系统中存在。
5. WHEN 一次导出嵌入多个图，THE Docx_Exporter SHALL 使每张内联图片与其对应 `Figure_Record` 的图题一一对应，不发生错配。

### Requirement 6: 结果表生成（grounded 于实验数据）

**User Story:** 作为论文作者，我希望系统把我的真实实验数据渲染成规范的结果表，以便论文用表格清晰呈现指标而无需我手工排版。

#### Acceptance Criteria

1. WHEN `Research_Artifact` 存在且某 `Experiment.results_data.stats` 非空，THE Table_Renderer SHALL 为该实验生成一张结果表，表中每个指标的数值取自 `stats` 的 mean/std/min/max，不生成任何不在该 `Experiment.results_data` 中的数值。
2. WHEN 输出格式为 LaTeX，THE Table_Renderer SHALL 产出 `table`/`tabular` 环境的结果表，含表头（指标、baselines/datasets 列）、`\caption` 与 `\label`。
3. WHEN 输出格式为 docx，THE Table_Renderer SHALL 产出 docx 原生表格元素，含表头行与数据行，而非把表内容合并入单个段落。
4. WHERE 某 `Experiment` 含 `baselines` 与 `metrics`，THE Table_Renderer SHALL 使结果表的行列结构反映 baselines 与 metrics 的对应关系，使每个 (baseline/方法, metric) 单元格取自对应的 `results_data` 数值。
5. THE Table_Renderer SHALL 使结果表中出现的每个数值均为 Grounded_Value（可在 `Research_Artifact` 数值集合及 `stats` 衍生值中按既有 Quality_Gate 容差找到），与既有 artifact-grounding 检查一致。
6. IF `Research_Artifact` 不存在或全部 `Experiment.results_data.stats` 为空，THEN THE Table_Renderer SHALL 不生成任何结果表，并记录「无可用实验数据，跳过表格生成」，且不失败。
7. WHEN Table_Renderer 格式化浮点数值，THE Table_Renderer SHALL 采用一致的小数位数呈现，并对由 `stats` 派生的文本（如列名）做长度上限为 500 字符的防御式截断。
8. THE Table_Renderer SHALL 将 `results_data` 内容视为不可信数据，不执行 `eval`/`exec`，且不因单条异常数据（缺字段/非数值）中止整表生成，而是跳过该单元格并记录。

### Requirement 7: 从实验数据生成图表（Figures-from-Data，P1）

**User Story:** 作为论文作者，我希望系统能把我的实验数据画成图，以便论文以图形直观呈现结果，并让这些图被真实嵌入导出文档。

#### Acceptance Criteria

1. WHERE 数据出图功能被配置启用且 `Research_Artifact` 存在含非空 `results_data` 的 `Experiment`，THE Figure_Renderer SHALL 从该实验数据生成图像文件（`Figure_Asset`），并产生对应的 `Figure_Record`（含 `figure_id`、指向该图像文件的 `data_ref`）。
2. WHEN Figure_Renderer 生成图像，THE 图像中呈现的数据点数值 SHALL 全部为 Grounded_Value，取自对应 `Experiment.results_data`，不引入编造数据。
3. WHEN Figure_Renderer 产出 `Figure_Record` 与 `Figure_Asset`，THE Writing_Agent SHALL 仅经 `AgentResult.mutations` 通过 `Workspace_Repository` 写回该记录，不直接写工作区。
4. WHEN Figure_Renderer 产出的 `Figure_Asset` 可被导出阶段定位，THE Latex_Exporter 与 Docx_Exporter SHALL 依 Requirement 4 与 Requirement 5 将其真实嵌入产物。
5. IF 数据出图所需的可选绘图依赖不可用，THEN THE Figure_Renderer SHALL 跳过数据出图、保留既有的图题文字说明行为（`writing_agent._process_figures`），并记录「绘图依赖不可用，已降级为文字图题」，且不中止管线。
6. WHERE 数据出图功能被配置禁用，THE Figure_Renderer SHALL 不生成任何图像文件，系统保留既有的文字图题行为。
7. WHEN Figure_Renderer 调用外部绘图工具或 LLM，THE 系统 SHALL 经既有可观测性与用量统计系统记录该调用。

### Requirement 8: 数字 grounding 与既有质量闸一致

**User Story:** 作为系统维护者，我希望模板、图、表引入的所有数字都受既有 grounding 约束，以便输出不引入编造数据。

#### Acceptance Criteria

1. THE Table_Renderer 与 Figure_Renderer SHALL 仅使用来自 `Research_Artifact` 的数值，使这些数值满足既有 Quality_Gate 的 artifact-grounding 判定（不触发 `fabricated_metric`）。
2. WHEN 结果表或数据图被生成并纳入工作区/产物，THE 系统 SHALL 使其中数值可被既有 Quality_Gate 的 grounding 检查视为 Grounded_Value。
3. IF 某待渲染数值无法在 `Research_Artifact` 数值集合中按既有容差找到，THEN THE 系统 SHALL 不将该数值写入表格或图，并记录该数值被拒绝的原因。
4. THE 本特性 SHALL 不放宽或绕过既有 Quality_Gate 的 grounding 检查阈值与判定路径。

### Requirement 9: 契约保持、原子落盘与依赖倒置

**User Story:** 作为系统架构师，我希望本特性所有新增组件遵循既有契约，以便保持依赖倒置、单一写入路径、原子落盘与断点续跑。

#### Acceptance Criteria

1. THE 本特性新增的所有智能体逻辑（图/表/记录的产出） SHALL 仅经 `AgentResult.mutations` 通过 `Workspace_Repository` 落盘，不调用绕过 `Workspace_Repository` 的工作区写入接口。
2. THE Template_Engine、Table_Renderer 与 Figure_Renderer SHALL 依赖抽象接口（`Document_Exporter` 协议、`LLMProvider`、检索/工具注册表），不在 Orchestrator 业务代码中实例化具体实现类。
3. WHEN 导出阶段落盘 `Figure_Asset`、`Style_Asset` 与目标格式文档，THE 系统 SHALL 使 `Export_Result.files` 列出的每个路径在文件系统中均存在。
4. WHEN 管线中断后重启，THE Workspace_Repository SHALL 从最近一次成功提交状态恢复，使已生成的 `Figure_Record` 与章节内容逐字节比对不变且不重复落盘。
5. IF 落盘失败，THEN THE Workspace_Repository SHALL 回滚到写入前状态，不留部分写入中间产物。
6. THE 本特性 SHALL 不修改既有 `Pandoc_Pipeline`、`Format_Gate` 或 `Format_Repair_Loop` 的职责，仅作为其上游（产出待转换/编译的模板脚手架、图像、表格标记与 Normalized_Markdown 构造）。

### Requirement 10: 优雅降级、可观测性与不可信数据处理

**User Story:** 作为系统操作者，我希望本特性在缺依赖或数据异常时优雅降级并可观测，以便系统始终产出尽可能可用的结果且行为可追溯。

#### Acceptance Criteria

1. WHEN 模板、图片嵌入、表格或数据出图任一子功能因依赖缺失或数据异常无法完成，THE 系统 SHALL 降级到既有行为（默认模板 / 无 includegraphics 回退 / 跳过表格 / 文字图题）并继续导出，而非中止整个管线。
2. WHEN 任一子功能发生降级，THE 系统 SHALL 经既有可观测性系统记录一条含子功能名与降级原因的事件。
3. WHEN 本特性调用任何外部工具（绘图、样式资产处理）或 LLM，THE 系统 SHALL 经既有可观测性与用量统计系统记录事件与用量。
4. THE 可观测记录 SHALL 不打印 API 密钥、完整请求体，且对记录的文本片段施加长度上限为 2000 字符。
5. THE 系统 SHALL 将外部工具输出与 LLM 输出视为不可信数据，不执行 `eval`/`exec`，并在解析前对超过 8000 字符的此类输出做防御式截断。
6. IF 外部工具或 LLM 调用失败、超时或无有效响应，THEN THE 系统 SHALL 丢弃该次结果、保持工作区字节级不变、记录失败原因，并按本 Requirement 第 1 条降级。
