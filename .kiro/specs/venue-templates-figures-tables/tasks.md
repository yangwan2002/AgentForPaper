# Implementation Plan: venue-templates-figures-tables（会议模板 + 真实图片嵌入 + 结果表生成 + 数据出图）

## Overview

本计划把设计文档拆解为增量式、测试驱动的编码任务，严格复用既有契约：单一写入路径（`AgentResult.mutations` 经 `WorkspaceRepository.update` 原子落盘）、依赖倒置（`DocumentExporter` 协议 / `get_exporter` 工厂、`LLMProvider`、绘图后端抽象）、grounding 不放宽（复用 `tools/quality_gate.py` 的判定），不改动 format-pipeline 组件。

任务顺序遵循依赖：先扩展底层契约与数据模型（grounding helper、`ExportResult.notes`、`FigureRecord` 字段、事件/配置），再建 Venue 注册表、`GroundingChecker`、路径防御、`Template_Engine`、`Table_Renderer`、`Figure_Renderer`，最后把它们分别接入 `LatexExporter` / `DocxExporter` / `WritingAgent` 并整体总装。

实现语言：Python（设计文档使用具体 Python，沿用仓库现有栈）。属性测试使用 Hypothesis（仓库已有 `.hypothesis/` 缓存），每条属性一个测试、最少 100 次迭代（`@settings(max_examples=100)`），并以注释标注：
`# Feature: venue-templates-figures-tables, Property N: {property_text}`。

标注 `*` 的子任务为可选测试任务（单元/属性/集成），可为快速 MVP 跳过；顶层任务不加 `*`。

## Tasks

- [x] 1. 抽取并复用 grounding helper（`tools/quality_gate.py`）
  - [x] 1.1 将 `QualityGate._check_artifact_grounding` 内构造 `extended_allowed` 的逻辑与 `_value_matches` 提升为模块级函数 `build_allowed_values(artifact) -> list[float]` 与 `value_matches(extracted, allowed, tolerance=0.01) -> bool`
    - `build_allowed_values` = `artifact.all_numeric_values()` ∪ 每指标 `stats` 的 `{mean, mean±std, min, max}` 衍生集合（逐字节复刻现有构造顺序与去重）
    - 让 `QualityGate._check_artifact_grounding` 与 `QualityGate._value_matches` 改为委托这两个模块级函数，**不改变任何现有 grounding 行为与容差**
    - _Requirements: 8.4_
  - [ ]* 1.2 编写 Property 3 属性测试
    - **Property 3: grounding 允许集合与既有质量闸同源一致**
    - 随机构造 `ResearchArtifact`，断言 `build_allowed_values(artifact)` 与既有 `QualityGate._check_artifact_grounding` 内部构造的 `extended_allowed` 集合逐元素相等
    - _Validates: Requirements 8.4_
  - [ ]* 1.3 编写回归单元测试
    - 用既有 fabricated_metric 用例断言重构后 `QualityGate.check` 的输出与重构前一致（行为不变）
    - _Requirements: 8.4_

- [x] 2. 扩展导出契约与工作区数据模型（向后兼容）
  - [x] 2.1 为 `ExportResult`（`export/base.py`）新增 `notes: list[str] = field(default_factory=list)` 字段，默认空、向后兼容
    - _Requirements: 3.2_
  - [x] 2.2 为 `FigureRecord`（`workspace/models.py`）新增可选字段 `source_experiment_id: str = ""` 与 `rendered_from_data: bool = False`；将 `PaperWorkspace.from_dict` 中 `figures=[FigureRecord(**f) ...]` 改为忽略未知键的安全构造（仅取 dataclass 已声明字段），保证旧 JSON 反序列化兼容
    - _Requirements: 7.1, 9.4_
  - [ ]* 2.3 编写向后兼容单元测试
    - 断言缺少新键的旧 `figures` JSON 能被 `from_dict` 正确反序列化；断言含未知键的 dict 不抛错；断言 `to_dict`→`from_dict` 往返一致
    - _Requirements: 7.1, 9.4_

- [x] 3. 扩展观测事件与运行配置
  - [x] 3.1 在 `observability/events.py` 的 `EventKind` 新增 `DEGRADATION = "degradation"` 与 `EXPORT_ASSET = "export_asset"`，并在 `observability/console.py` 增加对应渲染分支（保持既有 sink 契约不变）
    - _Requirements: 10.1, 10.2_
  - [x] 3.2 在 `config.py` 的 `Config` 新增 `venue_id: str = "default"`、`figures_from_data_enabled: bool = True`、`figure_float_decimals: int = 3`
    - `Venue_Id` 选择优先级：`ws.profile["venue_id"]` > `config.venue_id` > `"default"`（在后续导出/写作接入处消费）
    - _Requirements: 1.1, 1.2, 6.7, 7.6_

- [x] 4. 会议档案数据模型与注册表
  - [x] 4.1 新建 `export/venue_profiles.py`：定义纯数据 `StyleAsset`（`name`/`builtin_path`/`kind`）与 `VenueProfile`（`venue_id`/`document_class`/`class_options`/`style_assets`/`required_structure`/`docx_conventions`）及 `VenueProfile.is_valid()`（`document_class` 非空且 `required_structure` 完整）
    - _Requirements: 1.4, 2.1, 2.5_
  - [x] 4.2 新建 `export/venue_registry.py`：实现 `VenueRegistry.resolve(venue_id) -> VenueProfile | None`（未注册返回 `None`）与 `registered_ids()`；登记内置档案 `neurips`/`icml`/`acl`/`ieee`/`default`；`default` 档案设为 `document_class="article"`、`style_assets=[inputenc, graphicx]`（引用声明、无内置文件），以便回退到 `default` 时逐字节复现今日 `\documentclass{article}` 输出
    - _Requirements: 1.1, 1.5, 3.1_
  - [ ]* 4.3 编写注册表单元测试
    - 断言 `registered_ids()` 至少含 `{neurips, icml, acl, ieee, default}`（Req 1.5）；断言对每个已注册 id，`resolve(id).venue_id == id`；断言 `default` profile 的 `document_class == "article"`（Req 1.2）；未注册 id 返回 `None`
    - _Requirements: 1.2, 1.5_

- [x] 5. Checkpoint - 确保底层扩展测试通过
  - Ensure all tests pass, ask the user if questions arise.

- [x] 6. GroundingChecker 复用适配器
  - [x] 6.1 新建 `export/grounding.py`：`GroundingChecker(artifact)`，`allowed_values()` 委托 `quality_gate.build_allowed_values(artifact)`，`is_grounded(value, tolerance=0.01)` 委托 `quality_gate.value_matches`——不新增、不放宽任何判定路径
    - _Requirements: 8.1, 8.4_

- [x] 7. 图像路径穿越防御
  - [x] 7.1 新建 `export/asset_paths.py`：`safe_relative_asset(out_dir, candidate) -> str | None`——把 `candidate` 规整为绝对路径后，仅当其位于 `out_dir`（含资产子目录）之内时返回相对 `out_dir` 的相对路径，否则返回 `None`（视为缺资产回退）
    - _Requirements: 4.5_
  - [ ]* 7.2 编写 Property 13 属性测试
    - **Property 13: 图像路径穿越防御**
    - 用路径生成器（相对路径、`../` 穿越、绝对路径、目录内合法路径）断言：返回值要么为 `None`，要么是规整后仍位于 `out_dir` 之内的相对路径；任何情况都不返回指向目录外的路径
    - _Validates: Requirements 4.5_

- [x] 8. Template_Engine（模板引擎，纯数据、不调用 LLM）
  - [x] 8.1 新建 `export/template_engine.py`：`Scaffold` dataclass（`document_class`/`preamble_lines`/`asset_files`/`degraded`/`degrade_note`/`requested_venue_id`/`fallback_reason`/`aborted`）与 `TemplateEngine(registry, sink)`；实现 `build_scaffold(venue_id, out_dir)`
    - 解析→（单次、不级联回退，目标固定 `default`）→落盘内置 `Style_Asset` 到 `out_dir`→返回 `Scaffold`
    - 回退触发：`unregistered_venue`（`resolve` 为 `None`）/ `missing_style_asset`（声明的资产无内置文件也无可解析引用）/ `invalid_profile`（`is_valid()` 失败）
    - 回退时 `Scaffold.degraded=True`，`degrade_note` 为逐字节固定文本「已降级：请求的会议模板不可用，已回退到默认模板」，并经 `sink` 发出恰一条 `DEGRADATION` 事件（含 `venue_id` 与枚举回退原因）
    - `default` 亦不可用 → `aborted=True` 并发 `ERROR` 事件（唯一中止分支）
    - 落盘的样式文件路径经 `EXPORT_ASSET` 记录；样式引用名写入前截断至 500 字符；全程不调用任何 `LLMProvider`
    - _Requirements: 1.4, 2.1, 2.2, 2.3, 2.5, 3.1, 3.2, 3.4, 3.5, 3.6_
  - [ ]* 8.2 编写 Property 5 属性测试
    - **Property 5: 脚手架结构完整性**
    - 对任意已注册 `VenueProfile`，断言 `build_scaffold` 产出的脚手架包含该 profile 声明的全部必需结构元素（文档类声明、每个 `Style_Asset` 引用声明、标题/作者区、正文区）
    - _Validates: Requirements 1.4, 2.1_
  - [ ]* 8.3 编写 Property 6 属性测试
    - **Property 6: 样式资产引用名与落盘文件一致且受截断**
    - 对带内置文件的 `Style_Asset`：断言资产落盘于 `out_dir` 内、文件存在、出现在返回的 `asset_files` 中，且引用名与落盘 basename 一致；对任意超长引用名，写入脚手架的引用名长度 ≤ 500
    - _Validates: Requirements 2.2, 2.3, 2.5_
  - [ ]* 8.4 编写 Property 8 属性测试
    - **Property 8: 回退/降级标注与事件一致且恰一条**
    - 对任一触发回退的请求，断言 `Scaffold.degrade_note` 与 `DEGRADATION` 事件文本逐字节相同、附带的 `venue_id` 一致、`fallback_reason ∈ {unregistered_venue, missing_style_asset, invalid_profile}`、回退目标恒为 `default` 且事件恰一条；对任一子功能降级恰记一条含子功能名与原因的事件
    - _Validates: Requirements 3.2, 3.5, 10.2_
  - [ ]* 8.5 编写 Property 9 属性测试
    - **Property 9: 回退过程不调用 LLM**
    - 注入一个记账 `LLMProvider`，对任意回退，断言其调用计数为 0
    - _Validates: Requirements 3.4_
  - [ ]* 8.6 编写 default 中止分支边界测试
    - 构造 `default` 亦不可用的注册表，断言 `build_scaffold` 返回 `aborted=True`、不落盘文档、发出 `ERROR` 事件、输入不被修改
    - _Requirements: 3.6_

- [x] 9. Table_Renderer（结果表渲染器）
  - [x] 9.1 新建 `export/table_renderer.py`：`TableFragment` dataclass 与 `TableRenderer(grounding, sink, float_decimals=3, max_field_chars=500)`
    - `render_latex(artifact) -> list[TableFragment]`：为每个含非空 `stats` 的 `Experiment` 产出含 `tabular`/`\caption`/`\label`/表头行的片段；行=baselines/方法、列=metrics，单元格取对应 `results_data` 数值（优先 `stats[metric].mean`）
    - `render_docx(artifact, document) -> int`：向 python-docx `Document` 追加原生表格（表头行+数据行），返回追加表数
    - 每个数值经 `grounding.is_grounded` 校验，未通过则跳过该单元格并记 `DEGRADATION`（reason=rejected_ungrounded_value）；单条异常数据（缺字段/非数值）只跳过该单元格并记 `DEGRADATION`（reason=cell_skipped），不中止整表
    - 无 artifact / 全空 stats → 返回空并记「无可用实验数据，跳过表格生成」；浮点统一 `float_decimals` 位；派生文本（列名等）截断 500；不 `eval`/`exec`
    - _Requirements: 6.1, 6.2, 6.3, 6.4, 6.5, 6.6, 6.7, 6.8, 8.1, 8.2, 8.3_
  - [ ]* 9.2 编写 Property 2 属性测试
    - **Property 2: 非 grounded 数值被拒绝并记录**
    - Experiment 生成器注入非 grounded 值与缺字段/非数值单元；断言这些数值不出现在产出表中、记录了拒绝原因，且不中止整表
    - _Validates: Requirements 6.8, 8.3_
  - [ ]* 9.3 编写 Property 15 属性测试
    - **Property 15: 结果表结构完整性**
    - 对含非空 stats、baselines、metrics 的 Experiment：断言 LaTeX 片段含 `tabular`/`\caption`/`\label`/表头行；docx 表格行数 ≥ 2（表头+数据）；行列结构反映 baselines×metrics 对应关系
    - _Validates: Requirements 6.2, 6.3, 6.4_
  - [ ]* 9.4 编写 Property 16 属性测试
    - **Property 16: 无数据时优雅跳过表格**
    - 对不存在 artifact 或全空 stats 的输入，断言 `render_latex` 返回 `[]`、记录跳过说明、不抛异常
    - _Validates: Requirements 6.6_
  - [ ]* 9.5 编写 Property 17 属性测试
    - **Property 17: 数值格式化一致性与派生文本截断**
    - 对任意浮点数值断言渲染文本采用配置的一致小数位数；对任意由 stats 派生文本断言写入长度 ≤ 500
    - _Validates: Requirements 6.7_

- [x] 10. Figure_Renderer 与绘图后端抽象
  - [x] 10.1 新建 `export/plotting.py`：定义 `PlottingBackend` 协议（`available: bool`、`bar_chart(title, labels, values, out_path)`）与基于 matplotlib 的默认实现（matplotlib 为**可选依赖**，惰性导入，缺失时 `available=False`）
    - _Requirements: 7.5, 7.7_
  - [x] 10.2 新建 `export/figure_renderer.py`：`RenderedFigure` dataclass 与 `FigureRenderer(backend, grounding, sink, tracker, enabled=True)`；实现 `render_from_artifact(artifact, assets_dir) -> list[RenderedFigure]`
    - 从 `Experiment.results_data` 出图，只把 `grounding.is_grounded` 通过的数据点写入图（被拒数值不入图并记 `DEGRADATION`）
    - 产出 `RenderedFigure`（含 `FigureRecord`：`figure_id`+相对 `data_ref`+`caption`+`source_experiment_id`+`rendered_from_data=True`）与落盘 `asset_path`
    - `enabled=False` / `backend.available=False` / 无数据 → 返回 `[]`（依赖不可用时记「绘图依赖不可用，已降级为文字图题」）；外部调用经 `tracker`/`sink` 记账；渲染器只产数据、**不写工作区**
    - _Requirements: 7.1, 7.2, 7.5, 7.6, 7.7, 8.3_
  - [ ]* 10.3 编写 Property 18 属性测试
    - **Property 18: 数据出图产出资产与记录**
    - 对含非空 `results_data` 的 Experiment、启用且后端可用时，断言产出 `RenderedFigure`，其 `FigureRecord` 含 `figure_id` 与指向已落盘文件的 `data_ref`，且文件存在
    - _Validates: Requirements 7.1_
  - [ ]* 10.4 编写 Property 25 属性测试
    - **Property 25: 外部调用失败时工作区不变并降级**
    - 注入会失败/超时/无响应的绘图后端，断言丢弃该次结果、返回 `[]`（回落文字图题）、记录失败原因、不抛出使管线中止的异常
    - _Validates: Requirements 10.6_

- [x] 11. Checkpoint - 确保渲染器与引擎测试通过
  - Ensure all tests pass, ask the user if questions arise.

- [x] 12. LatexExporter 集成（`export/latex.py`）
  - [x] 12.1 改造 `LatexExporter`：注入 `TemplateEngine` 与 `TableRenderer`（经构造默认值，保持 `get_exporter` 无参构造兼容），改写 `_render_tex`
    - 用 `template_engine.build_scaffold(venue_id, out_dir)` 的 `document_class`/`preamble_lines` 替换硬编码 `\documentclass{article}`+`\usepackage`；`Scaffold.aborted` 时不写文件并返回中止信号
    - 图块：对每个 `FigureRecord`，经 `safe_relative_asset` 定位资产；有资产则在 `figure` 环境内先写 `\includegraphics{相对路径}` 再写 `\caption`/`\label`；无资产（含路径穿越）保留仅 `\caption`/`\label` 回退并记 `DEGRADATION`（missing_asset / unsafe_path）
    - 表块：调用 `table_renderer.render_latex(ws.artifact)` 追加 `table`/`tabular` 片段
    - 将降级标注写入 `ExportResult.notes`，落盘的样式/图像路径并入 `ExportResult.files`
    - _Requirements: 1.3, 2.2, 4.1, 4.2, 4.3, 4.4, 4.5, 4.6, 6.2_
  - [ ]* 12.2 编写 Property 4 属性测试
    - **Property 4: 会议档案解析一致性**
    - 对已注册 id 断言 `resolve(id).venue_id == id`；对非 `default` 已注册 profile 断言导出 `.tex` 首个 `\documentclass` 参数等于该 profile 的 `document_class`
    - _Validates: Requirements 1.1, 1.3_
  - [ ]* 12.3 编写 Property 7 属性测试
    - **Property 7: 模板回退产出完整目标文档**
    - 对触发任一不可用条件、`default` 可用的请求，断言回退后 `.tex` 的章节/图/表/参考文献数量与内容与直接以 `default` 导出逐一相同（仅样式回退）
    - _Validates: Requirements 3.1, 3.3, 2.4, 10.1_
  - [ ]* 12.4 编写 Property 10 属性测试
    - **Property 10: LaTeX 图嵌入顺序与一一对应**
    - 用图集合生成器（各带唯一资产）断言每个 `figure` 环境中 `\includegraphics` 位置严格早于 `\caption`/`\label`，且引用路径对应各自 `FigureRecord`，多图不错配
    - _Validates: Requirements 4.1, 4.6_
  - [ ]* 12.5 编写 Property 11 属性测试
    - **Property 11: LaTeX 图路径一致、存在且保留图题/标签**
    - 断言 `\includegraphics` 路径等于落盘资产相对路径、文件存在且在 `files` 中，同时仍产出 `\caption{escape(caption)}` 与 `\label{figure_id}`
    - _Validates: Requirements 4.2, 4.3, 9.3_
  - [ ]* 12.6 编写 Property 12 属性测试
    - **Property 12: 缺资产图的 LaTeX 回退**
    - 对无可定位资产的 `FigureRecord`，断言不生成 `\includegraphics`、仍产出仅含 `\caption`/`\label` 的回退，并记录缺资产
    - _Validates: Requirements 4.4_

- [x] 13. DocxExporter 集成（`export/docx.py`）
  - [x] 13.1 改造 `DocxExporter.export`：应用 `VenueProfile.docx_conventions`（标题层级等）；图表区改为——经 `safe_relative_asset` 定位资产则 `document.add_picture(资产)` 并在其下加图题段落，无资产回落既有 `figure_id: caption` 段落并记 `DEGRADATION`；表格区调用 `table_renderer.render_docx(ws.artifact, document)`；python-docx 不可用时以可诊断错误处理且不产出半损坏文件
    - _Requirements: 1.6, 5.1, 5.2, 5.3, 5.4, 5.5, 6.3_
  - [ ]* 13.2 编写 Property 14 属性测试
    - **Property 14: docx 图嵌入与图题一一对应**
    - 断言内联图片数=有资产图数，每图片下方为其对应图题，多图不错配；无资产图保留 `figure_id`+图题回退并记缺资产
    - _Validates: Requirements 5.1, 5.2, 5.5_
  - [ ]* 13.3 编写 python-docx 缺失边界测试
    - 模拟 python-docx 不可用，断言抛出指明缺失依赖的可诊断错误且不产出部分损坏的 docx 文件
    - _Requirements: 5.3_

- [ ] 14. 导出落盘属性与综合 grounding 属性
  - [ ]* 14.1 编写 Property 22 属性测试
    - **Property 22: 落盘路径存在性**
    - 对成功导出（LaTeX 与 docx），断言 `ExportResult.files` 列出的每个路径（文档、`Style_Asset`、`Figure_Asset`）在文件系统中存在
    - _Validates: Requirements 9.3_
  - [ ]* 14.2 编写 Property 1 属性测试
    - **Property 1: 表/图数值 grounding 不变式**
    - 对任意含非空 stats 的 Experiment，断言 `TableRenderer` 表格与传给绘图后端的每个数值都是 `Grounded_Value`（经 `quality_gate.value_matches` 判定），且将产物纳入工作区后 `QualityGate.check` 不产生针对表/图数值的 `fabricated_metric`
    - _Validates: Requirements 6.1, 6.5, 7.2, 8.1, 8.2_

- [x] 15. WritingAgent 集成 Figure_Renderer（单一写入路径）
  - [x] 15.1 在 `agents/writing_agent.py` 的 `_process_figures` 位置接入 `FigureRenderer`：启用且有数据时先尝试数据出图，产出的 `FigureRecord` 经 `AgentResult.mutations` 写回；失败/禁用/无后端回落既有 LLM 文字图题
    - 图像文件写入 `{workspace_dir}/{workspace_id}_assets/`，相对路径记入 `FigureRecord.data_ref`
    - 续跑幂等：按「图像文件已存在 + 记录已在工作区」跳过，不重复追加/落盘
    - _Requirements: 7.3, 9.1, 9.4_
  - [ ]* 15.2 编写 Property 19 属性测试
    - **Property 19: 单一写入路径不变式**
    - 断言在 mutation 被 `WorkspaceRepository` 应用前工作区 `figures` 逐字节不变，应用后含新 `FigureRecord`；无任何绕过 repository 的写入
    - _Validates: Requirements 7.3, 9.1_
  - [ ]* 15.3 编写 Property 20 属性测试
    - **Property 20: 绘图禁用/依赖不可用时降级为文字图题**
    - 对含数据的 Experiment，禁用或后端不可用时断言不产图像、回落文字图题、依赖不可用时记录降级、管线不中止
    - _Validates: Requirements 7.5, 7.6_
  - [ ]* 15.4 编写 Property 21 属性测试
    - **Property 21: 续跑幂等**
    - 对已含数据出图 `FigureRecord` 的工作区，重跑产图逻辑后断言已有记录与章节内容逐字节不变、不重复追加/落盘
    - _Validates: Requirements 9.4_
  - [ ]* 15.5 编写 Property 23 属性测试
    - **Property 23: 落盘失败回滚**
    - 注入会抛错的 store，断言 `WorkspaceRepository.update` 在 `store.save` 抛错后将工作区恢复到写入前状态（`to_dict()` 逐字节相等），不留部分写入
    - _Validates: Requirements 9.5_

- [ ] 16. 防御式截断与可观测属性
  - [ ]* 16.1 编写 Property 24 属性测试
    - **Property 24: 防御式截断（可观测与不可信输入）**
    - 用不可信文本生成器（>8000、>2000、>500）断言：经可观测系统记录的事件文本片段 ≤ 2000 且不含密钥/完整请求体；>8000 的外部输出解析前截断至 ≤ 8000 且不执行 `eval`/`exec`
    - _Validates: Requirements 10.4, 10.5_

- [ ] 17. 集成与架构断言（总装）
  - [ ]* 17.1 编写数据出图→导出端到端集成测试
    - 从含 `results_data` 的 artifact 经 `WritingAgent` 产图落盘，再经 `LatexExporter`/`DocxExporter` 嵌入，断言产物含真实图像/表格且路径存在
    - _Requirements: 7.4_
  - [ ]* 17.2 编写记账集成测试
    - 断言 `Figure_Renderer` 的绘图/LLM 调用经 `UsageTracker`/`EventSink` 记录事件与用量
    - _Requirements: 7.7, 10.3_
  - [ ]* 17.3 编写架构断言测试
    - 断言 Orchestrator `_export_phase` 仍只调用 `get_exporter(...).export`；`LatexExporter`/`DocxExporter` 满足 `DocumentExporter` 协议（`isinstance` 运行时协议检查）；本特性模块不 import/改动 format-pipeline 组件
    - _Requirements: 1.7, 9.2, 9.6_

- [x] 18. 最终 Checkpoint - 确保全部测试通过
  - Ensure all tests pass, ask the user if questions arise.

## Notes

- 标注 `*` 的子任务为可选测试任务，可为快速 MVP 跳过；核心实现子任务不得跳过。
- 每个任务都引用了具体的需求子条款以保证可追溯性；属性测试子任务显式引用设计文档中的对应 Property。
- Checkpoint 用于分阶段增量验证；grounding、单一写入路径与回滚属性复用既有 `quality_gate` / `WorkspaceRepository` 作为 oracle，避免重复实现容差与快照逻辑。
- 属性测试统一使用 Hypothesis、最少 100 次迭代，并按约定注释标注 Property 编号与文本；docx 属性在 python-docx 可用时运行、不可用时跳过并由边界测试覆盖缺依赖分支。
- 25 条 Correctness Properties 与测试子任务映射：P1→14.2、P2→9.2、P3→1.2、P4→12.2、P5→8.2、P6→8.3、P7→12.3、P8→8.4、P9→8.5、P10→12.4、P11→12.5、P12→12.6、P13→7.2、P14→13.2、P15→9.3、P16→9.4、P17→9.5、P18→10.3、P19→15.2、P20→15.3、P21→15.4、P22→14.1、P23→15.5、P24→16.1、P25→10.4。

## Task Dependency Graph

```json
{
  "waves": [
    { "id": 0, "tasks": ["1.1", "2.1", "2.2", "3.1", "3.2", "4.1", "7.1"] },
    { "id": 1, "tasks": ["1.2", "1.3", "2.3", "4.2", "6.1", "7.2", "10.1"] },
    { "id": 2, "tasks": ["4.3", "8.1", "9.1", "10.2"] },
    { "id": 3, "tasks": ["8.2", "8.3", "8.4", "8.5", "8.6", "9.2", "9.3", "9.4", "9.5", "10.3", "10.4", "12.1", "13.1"] },
    { "id": 4, "tasks": ["12.2", "12.3", "12.4", "12.5", "12.6", "13.2", "13.3", "14.1", "14.2", "15.1"] },
    { "id": 5, "tasks": ["15.2", "15.3", "15.4", "15.5", "16.1", "17.1", "17.2", "17.3"] }
  ]
}
```
