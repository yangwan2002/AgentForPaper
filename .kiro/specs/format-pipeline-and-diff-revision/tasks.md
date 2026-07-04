# Implementation Plan: format-pipeline-and-diff-revision（补丁优先增量修订 + Markdown 内容契约与 pandoc 导出管线）

## Overview

本计划把设计文档拆解为增量式、测试驱动的编码任务，严格复用既有契约：单一写入路径（`AgentResult.mutations` 经 `WorkspaceRepository.update` 原子落盘）、依赖倒置（`DocumentExporter` 协议 / `get_exporter` 工厂、`LLMProvider`、`ToolRegistry`）、`Format_Gate` 全程不调用 LLM、把外部工具/LLM 输出视为不可信数据（不 `eval`/`exec`、防御式截断），并经既有 `EventSink` / `UsageTracker` 记录事件与用量。不重新设计 sibling 特性 venue-templates-figures-tables 的组件（`TemplateEngine`/`Scaffold`、`TableRenderer`、`FigureRenderer`、`GroundingChecker`、`safe_relative_asset`、`\includegraphics` 图嵌入、原生 docx 图/表），仅在既有导出流程中把「章节正文渲染」一段替换为 pandoc 转换。

任务顺序遵循依赖：先落配置与数据模型（Part A/B config、路由/报告/修复 dataclass），再建 `Content_Contract` 与 `PandocConverter` 两个基础组件，然后重构 `WritingAgent` 修订路径（Part A），改造三个导出器接入 pandoc（Part B），新建 `Format_Gate` 与 `Format_Repair_Loop`，最后接入 `Orchestrator._export_phase` 总装并补文档。

实现语言：Python（设计文档使用具体 Python，沿用仓库现有栈）。属性测试使用 Hypothesis（仓库已有 `.hypothesis/` 缓存），每条 Correctness Property 用**单个**属性测试实现、最少 100 次迭代（`@settings(max_examples=100)`），并以注释标注：
`# Feature: format-pipeline-and-diff-revision, Property N: {property_text}`。
属性测试中 pandoc/pdflatex 与 LLM 均以可控 stub/mock 注入（按退出码/超时/输出驱动），无需真实外部工具。

标注 `*` 的子任务为可选测试任务（单元/属性/集成），可为快速 MVP 跳过；顶层任务不加 `*`。

## Tasks

- [ ] 1. 扩展运行配置（Part A + Part B）与装配层校验
  - [ ] 1.1 在 `config.py` 的 `Config` 新增字段并附取值说明
    - Part A：`patch_first_enabled: bool = True`（Req 1.7）、`patch_size_limit: float = 0.5`（Req 3.2，取值 0.0–1.0）
    - Part B：`pandoc_degrade_strategy: str = "fallback"`（Req 8.6，取值 `{fallback, fail_fast}`）、`pandoc_probe_timeout: float = 5.0`（Req 8.1）、`enable_pdflatex_check: bool = False`（Req 9.2）、`format_gate_timeout: int = 60`（Req 9.7，取值 1–600）、`max_repair_attempts: int = 3`（Req 10.3/11.1，取值 0–10）
    - _Requirements: 1.7, 3.2, 8.1, 8.6, 9.2, 9.7, 10.3, 11.1_
  - [ ] 1.2 在装配层（`config.py` 内新增 `Config.validate()` 或 provider 构建处）实现范围校验
    - `patch_size_limit ∈ [0.0, 1.0]`、`format_gate_timeout ∈ [1, 600]`、`max_repair_attempts ∈ [0, 10]`、`pandoc_degrade_strategy ∈ {fallback, fail_fast}`；越界/非法以 1–500 字符错误拒绝并指明允许取值
    - _Requirements: 3.2, 8.6, 9.7, 10.3_
  - [ ]* 1.3 编写 Property 27 属性测试
    - **Property 27: 非法降级策略被拒**
    - 随机生成不属于 `{fallback, fail_fast}` 的字符串，断言校验以 1–500 字符错误拒绝并含允许取值
    - _Validates: Requirements 8.6_
  - [ ]* 1.4 编写配置范围校验单元测试
    - 覆盖 `patch_size_limit`、`format_gate_timeout`、`max_repair_attempts` 的边界（下界、上界、越界）
    - _Requirements: 3.2, 9.7, 10.3_

- [ ] 2. 新增 Part A 路由与补丁应用数据模型（`workspace/models.py` 或新建 `agents/revision_types.py`）
  - [ ] 2.1 定义 `RevisionRoute`（`PATCH_MODE`/`WHOLE_SECTION`）、`FallbackReason`（`ANCHOR_NOT_UNIQUE`/`STRUCTURAL_CHANGE`/`PATCH_SIZE_EXCEEDED`）枚举与 `PatchApplication` dataclass（`section_id`/`applied`/`skipped`/`route`/`fallback_reason`/`changed_intervals`）
    - 复用既有 `SectionEdit`（`workspace/models.py`），不修改其字段
    - _Requirements: 4.2, 4.3, 2.5_
  - [ ]* 2.2 编写数据模型单元测试
    - 断言枚举值字符串取值稳定、`PatchApplication` 默认字段可构造
    - _Requirements: 4.2, 4.3_

- [ ] 3. 新增 Part B 数据模型（`export/format_models.py`）
  - [ ] 3.1 定义 `ContractViolation`、`NormalizeResult`（Content_Contract）；`ToolRunResult`、`OffendingFragment`、`FormatGateReport`（Format_Gate）；`RepairTerminalStatus`、`RepairAttempt`、`RepairOutcome`（修复循环）——纯 dataclass/Enum、零外部依赖
    - 字段与设计 Data Models 一致，含 `stderr_excerpt ≤2000`、`excerpt ≤500`、`offending_fragments ≤10`、`tool_runs ≤ max+1` 等约束的语义注释
    - _Requirements: 5.9, 9.4, 10.8, 11.1, 11.6_
  - [ ]* 3.2 编写数据模型单元测试
    - 断言各 dataclass 默认构造、枚举取值稳定、`FormatGateReport.passed` 可独立赋值
    - _Requirements: 9.4, 11.1_

- [ ] 4. 实现 Content_Contract（新建 `export/content_contract.py`）
  - [ ] 4.1 实现 `normalize(content) -> NormalizeResult`
    - 归一化为受约束 Normalized_Markdown 子集（段落/ATX 标题/列表/强调/行内与围栏代码/行内 `$...$` 与块级 `$$...$$` 数学/图片图表引用/表格/`[id]` 引用）；数学定界符内部内容不施加任何转义（Req 5.4）；实现为字节级幂等 `normalize(normalize(x)) == normalize(x)`（Req 5.7）；`changed` 标记是否发生改写；绝不静默丢弃内容
    - _Requirements: 5.1, 5.4, 5.7_
  - [ ] 4.2 实现 `validate(content, ws) -> list[ContractViolation]`
    - 引用 `[id]` 不在 `ws.verified_reference_ids()` → `unknown_citation` 诊断并保留原文（Req 5.5）；`figure_id` 未唯一对应一条 `FigureRecord` → `unknown_figure`（Req 5.6）；长度 > 1,000,000 字符 → `length_exceeded` 诊断但不截断（Req 5.8）；契约外构造 → 归一化为等价表示或 `unknown_construct` 诊断（含 `offset` 或 `line/column`、`excerpt ≤500`），绝不静默丢弃（Req 5.9）
    - _Requirements: 5.5, 5.6, 5.8, 5.9_
  - [ ]* 4.3 编写 Property 9 属性测试
    - **Property 9: normalize 字节级幂等**
    - 生成含 Unicode/数学/代码/`[id]`/列表/标题的输入，断言 `normalize(normalize(x)).content == normalize(x).content` 字节级相等
    - _Validates: Requirements 5.7_
  - [ ]* 4.4 编写 Property 10 属性测试
    - **Property 10: 归一化保留内容、绝不静默丢弃**
    - 生成含契约外构造、库外 `[id]`、超 1,000,000 字符的输入，断言要么归一化为等价表示、要么产出含 `offset`/`line-column` 的诊断，且原始内容不被截断/丢弃
    - _Validates: Requirements 5.5, 5.8, 5.9_
  - [ ]* 4.5 编写 Property 11 属性测试
    - **Property 11: 产物符合内容契约**
    - 生成契约内构造的内容，断言 `normalize` 后 `validate` 不产生 `unknown_construct` 诊断
    - _Validates: Requirements 5.1, 5.2, 5.3_
  - [ ]* 4.6 编写 Property 13 属性测试
    - **Property 13: figure 引用唯一对应**
    - 生成 `figure_id` 与工作区 `FigureRecord` 集合的组合（无对应/唯一对应/多重对应），断言无/多对应时产出可诊断项、唯一对应时通过
    - _Validates: Requirements 5.6_

- [ ] 5. 实现 PandocConverter（新建 `export/pandoc_pipeline.py`）
  - [ ] 5.1 实现 `PandocConverter.probe(timeout=5.0) -> bool` 与 `convert(markdown, target, out_path=None, timeout) -> ConversionResult`
    - `probe`：探测 pandoc 可执行且能返回版本号，超时/未返回版本 → 判不可用（Req 8.1）；`convert`：用**参数列表**（非 shell 字符串）调用 `subprocess` 避免注入（Req 12.9），非零退出 → `ConversionResult(ok=False, exit_code, stderr≤2000)`（Req 6.4）；输出视为不可信数据、防御式截断
    - 定义 `ConversionResult` dataclass（`ok`/`exit_code`/`stderr`/`output_path`/`content`）
    - _Requirements: 6.1, 6.4, 8.1, 12.9_
  - [ ]* 5.2 编写 PandocConverter 单元测试（stub subprocess）
    - 断言 probe 超时判不可用、非零退出返回 `ok=False` 且 `stderr` 截断 ≤2000、调用参数为列表形式（不经 shell）
    - _Requirements: 6.4, 8.1, 12.9_

- [ ] 6. Checkpoint - 确保配置、数据模型与两个基础组件测试通过
  - Ensure all tests pass, ask the user if questions arise.

- [ ] 7. 重构 WritingAgent 补丁优先修订路由（`agents/writing_agent.py`）
  - [ ] 7.1 新增 `_route_revision(ws, section_id, suggestion, structural) -> RevisionRoute` 并接入 `_localized_revision`
    - `extras["structural"]` 指向本章节 → `WHOLE_SECTION`（Req 3.2）；否则默认 `PATCH_MODE`（Req 1.1/1.7）；保留既有工具循环产出 `SectionEdit` 意图的流程；运行期回退判据（全锚点失败/超 `patch_size_limit`/结构型）在 7.2/7.3 落地
    - 保证三态终止：`{已应用补丁, 完成整章重写, 无可应用变更且不改动工作区}`（Req 3.5）
    - _Requirements: 1.1, 1.7, 3.1, 3.2, 3.5_
  - [ ]* 7.2 编写 Property 6 属性测试
    - **Property 6: 修订路由完备且终止**
    - 生成含/不含 structural、锚点全失败、补丁占比超阈值等输入，断言有限步内恰达三态之一，且回退条件正确触发 `WHOLE_SECTION`
    - _Validates: Requirements 1.1, 3.1, 3.2, 3.5_

- [ ] 8. 增强 _materialize_edits 的改动区间重叠检测（`agents/writing_agent.py`）
  - [ ] 8.1 在 `_materialize_edits` 中维护已成功补丁的改动字符区间集合，重叠则跳过并记录「锚点区间冲突已跳过」
    - 逐条在当前内容重新定位锚点、仅唯一命中（次数==1）时应用（Req 2.4）；命中 0/多跳过并记录含实际命中次数的原因（Req 1.5/2.4）；累计影响占比 > `patch_size_limit` → 触发整章重写回退信号（Req 3.2）；仅经 `AgentResult.mutations` 写回目标章节（Req 1.6）
    - _Requirements: 1.4, 1.5, 2.3, 2.4, 2.5, 1.6_
  - [ ]* 8.2 编写 Property 1 属性测试
    - **Property 1: 未触及文本的字节级保留**
    - 生成章节内容与成功补丁集合，断言未被任一成功补丁锚点区间覆盖的字符序列修订前后字节级相同，且非目标章节 `content` 字节级不变
    - _Validates: Requirements 2.1, 2.2, 3.3_
  - [ ]* 8.3 编写 Property 2 属性测试
    - **Property 2: 锚点唯一性门控**
    - 生成锚点 0/1/多命中的补丁，断言仅命中==1 时应用、否则跳过且已成功补丁不变，并在 logs 记录含实际命中次数的原因
    - _Validates: Requirements 1.4, 1.5, 2.4_
  - [ ]* 8.4 编写 Property 3 属性测试
    - **Property 3: 补丁改动区间不重叠**
    - 生成锚点区间重叠的补丁序列，断言重叠补丁被跳过、当前内容字节级不变，并记录「锚点区间冲突已跳过」
    - _Validates: Requirements 2.5_
  - [ ]* 8.5 编写 Property 4 属性测试
    - **Property 4: 精确编辑的字节语义**
    - 生成唯一命中锚点的 replace/insert_after/insert_before（含空 `replacement`），断言字节语义正确且其余字符字节级不变
    - _Validates: Requirements 2.7, 2.8_

- [ ] 9. 校验非法 mode 拒绝（`tools/section_edit_tool.py` 复核 + 测试）
  - [ ] 9.1 复核 `SectionEditTool.edit_section` 对非法 `mode` 的拒绝路径，确保拒绝时目标章节内容字节级不变并返回指示 mode 非法的错误（既有实现已满足，补充断言点/日志）
    - _Requirements: 1.3_
  - [ ]* 9.2 编写 Property 5 属性测试
    - **Property 5: 非法 mode 拒绝且内容不变**
    - 生成不属于 `{replace, insert_after, insert_before}` 的 mode，断言 `edit_section` 拒绝、内容字节级不变、返回 mode 非法错误
    - _Validates: Requirements 1.3_

- [ ] 10. 增强 Whole_Section_Regeneration 的契约校验与失败处理（`agents/writing_agent.py`）
  - [ ] 10.1 在 `_revise_content`（整章重写）产出后调用 `Content_Contract.normalize/validate` 校验
    - 不合规 → 丢弃输出、目标章节 `content` 字节级不变、记录可诊断原因（Req 3.6）；LLM 调用失败/超时 → 不改动工作区并记录原因（Req 3.7）；产物符合 Content_Contract（Req 3.4）；仅经 `AgentResult.mutations` 写回（Req 3.3）
    - _Requirements: 3.3, 3.4, 3.6, 3.7_
  - [ ]* 10.2 编写 Property 7 属性测试
    - **Property 7: 整章重写不合规则保留原文**
    - stub LLM 产出不合规内容，断言输出被丢弃、目标章节 `content` 字节级不变、记录可诊断原因
    - _Validates: Requirements 3.6_
  - [ ]* 10.3 编写整章重写失败路径单元测试
    - stub LLM 抛错/超时，断言不写工作区并记录原因（Req 3.7）；stub 合规产出断言契约校验被调用（Req 3.4）
    - _Requirements: 3.4, 3.7_

- [ ] 11. 修订差量可观测与用量记录（`agents/writing_agent.py`）
  - [ ] 11.1 在补丁应用/回退处经 `EventSink`（复用 `EventKind.AGENT_LOG`）发出修订路径与 diff 决策事件
    - Patch_Mode 应用 ≥1 补丁 → 载荷含 `section_id`、成功/跳过计数（Req 4.1）；回退 → 标识 `fallback_reason ∈ {锚点未唯一命中, 结构型改动, 超过补丁适用上限}`（Req 4.2）；记录本轮路径 `∈ {Patch_Mode, Whole_Section_Regeneration}`（Req 4.3）；载荷不含 API 密钥/完整请求体，正文片段 ≤2000 字符（Req 4.4）；经既有 `UsageTracker` 记录 token 用量（Req 4.5）
    - _Requirements: 4.1, 4.2, 4.3, 4.4, 4.5_
  - [ ]* 11.2 编写 Property 8 属性测试
    - **Property 8: 修订可观测载荷正确且脱敏**
    - 生成一轮修订，断言事件载荷含正确路径/section_id/计数、回退原因取值合法、无密钥或完整请求体、正文片段 ≤2000 字符
    - _Validates: Requirements 4.1, 4.2, 4.3, 4.4_

- [ ] 12. Checkpoint - 确保 Part A 修订路径测试通过
  - Ensure all tests pass, ask the user if questions arise.

- [ ] 13. 重构 LatexExporter 接入 pandoc 片段转换（`export/latex.py`）
  - [ ] 13.1 在 `_render_tex` 中把章节正文的 `_escape(content)` 替换为 `PandocConverter.convert(section_md, target="latex")`
    - 保留 venue `Scaffold` 前导、`\section{title}`、图 `figure` 环境（`safe_relative_asset` + `\includegraphics`）、`TableRenderer` 的 `tabular` 片段、`.bib` 生成、章节末 `\cite` 回退；沿用 `[id]→\cite` 占位符保护技巧（转换前替换为 alnum 占位符、转换后还原）；数学 `$...$`/`$$...$$` 由 pandoc 正确转换（Req 6.3）；某章节非零退出 → 终止本次导出、不写部分/损坏文件、错误含失败章节标识（Req 6.4）
    - 保留现有手写 `_render_tex` 转义逻辑作为 pandoc 不可用时的**回退渲染器**（重命名/抽出为 `_render_tex_fallback`）
    - _Requirements: 6.1, 6.3, 6.4, 6.5_
  - [ ]* 13.2 编写 Property 15 属性测试
    - **Property 15: .bib 恰为已验证文献集合**
    - 生成工作区文献集合，断言 LaTeX 导出 `.bib` 条目集合恰等于已验证文献集合
    - _Validates: Requirements 6.5_

- [ ] 14. 重构 DocxExporter 接入 pandoc 结构化正文（`export/docx.py`）
  - [ ] 14.1 用 `PandocConverter.convert(combined_sections_md, target="docx")` 产出结构化 docx 主体，再以 python-docx 重新打开追加原生图/表/参考文献
    - 各章节以 ATX 标题 + 正文拼接为组合 Markdown 文档 → 标题/段落/列表/数学各映射为 docx 原生结构（Req 6.6）；随后按 sibling 既有逻辑 `add_picture`（`safe_relative_asset` 定位）+ 图题、`TableRenderer.render_docx`、参考文献段落追加；保留现有整段渲染逻辑作为 pandoc 不可用时的**回退渲染器**
    - _Requirements: 6.1, 6.6_
  - [ ]* 14.2 编写 DocxExporter 单元测试（stub PandocConverter）
    - 断言 pandoc 分支被调用、追加原生图/表/refs 逻辑不变、回退分支在 pandoc 不可用时启用
    - _Requirements: 6.1, 6.6_

- [ ] 15. 固化 MarkdownExporter 零外部依赖行为（`export/markdown.py`）
  - [ ] 15.1 复核并加固 `MarkdownExporter.export` 从不调用外部工具
    - 直接渲染，保留章节顺序/标题层级/正文字节（Req 7.4）；数学/代码/`[id]` 原样保留不转义（Req 7.6）；PATH 缺 pandoc/pdflatex 仍成功且不标注降级（Req 7.2）；空章节集合产出仅骨架 `.md` 不报错（Req 7.7）；图题/参考文献引用编号取值与排列一致（Req 7.5）
    - _Requirements: 7.1, 7.2, 7.4, 7.5, 7.6, 7.7_
  - [ ]* 15.2 编写 Property 14 属性测试
    - **Property 14: Markdown 直接渲染保真且零外部依赖**
    - 生成任意工作区，断言不调用任何外部可执行程序、章节顺序/标题层级/正文字节保留、数学/代码/`[id]` 不转义、缺工具仍成功不标降级、引用编号取值与顺序一致（以 mock/monkeypatch 拦截 subprocess 断言零调用）
    - _Validates: Requirements 7.1, 7.2, 7.4, 7.5, 7.6_
  - [ ]* 15.3 编写空工作区骨架单元测试
    - 断言空章节集合产出仅骨架 `.md` 且不报错
    - _Requirements: 7.7_

- [ ] 16. pandoc 不可用降级矩阵接入导出器（`export/latex.py`、`export/docx.py`）
  - [ ] 16.1 在 LatexExporter/DocxExporter 的 `export` 入口按 `pandoc_degrade_strategy` 决策
    - `probe` 不可用 + `fallback` → 回退手写渲染器并在 `ExportResult.notes` 与事件一致标注「已降级：pandoc 不可用」（Req 8.2）；`fallback` 且手写也无法产出 → 该格式以 1–500 字符错误失败并在 `ExportResult` 标注，保留其他格式（Req 8.3）；`fail_fast` → 以含安装指引的 1–500 字符错误失败（Req 8.4）
    - 经构造参数注入 `pandoc_degrade_strategy`/`pandoc_probe_timeout`（依赖注入，不在 exporter 内读全局 config）；保持 `export(ws, dir)` 签名与 `get_exporter` 工厂不变（Req 6.8）
    - _Requirements: 6.8, 8.2, 8.3, 8.4_
  - [ ]* 16.2 编写降级分支单元测试
    - stub probe=不可用，断言 `fallback` 走手写并标注、`fail_fast` 含安装指引错误、Markdown 从不调用 pandoc
    - _Requirements: 8.2, 8.4_

- [ ] 17. Checkpoint - 确保导出器改造测试通过
  - Ensure all tests pass, ask the user if questions arise.

- [ ] 18. 实现 Format_Gate（新建 `export/format_gate.py`）
  - [ ] 18.1 实现 `Format_Gate.check(artifacts, sections) -> FormatGateReport`
    - 对 LaTeX/docx 产物实际运行 pandoc（+ 当 `enable_pdflatex_check` 且 pdflatex 可用时运行 pdflatex），带 `format_gate_timeout`（1–600，默认 60）超时（Req 9.2/9.7）；`passed == all(exit_code==0) and not any(timed_out) and not missing_tools`（Req 9.3）；任一非零 → `passed=False` + 结构化报告（失败工具名/退出码/`stderr≤2000`/`OffendingFragment` 每段 ≤500 最多 10 段含行号或偏移）（Req 9.4）；超时 → `passed=False`、终止进程、记录超时工具与阈值（Req 9.7）；工具缺失 → `passed=False` 记缺失工具（Req 9.8）；**绝不调用任何 LLM**（Req 9.5）；不修改/删除原始产物（Req 9.9）；与 `Quality_Gate` 独立（Req 9.6）
    - _Requirements: 9.1, 9.2, 9.3, 9.4, 9.5, 9.6, 9.7, 9.8, 9.9_
  - [ ]* 18.2 编写 Property 17 属性测试
    - **Property 17: 格式闸以工具退出码为唯一裁判（无 LLM）**
    - 生成任意退出码/超时/缺失组合的工具结果 stub，断言 `passed` 当且仅当全部退出码 0 且无超时无缺失，且判定不注入任何 LLM（断言未调用 LLM stub）
    - _Validates: Requirements 9.1, 9.3, 9.4, 9.5, 9.8_
  - [ ]* 18.3 编写 Property 18 属性测试
    - **Property 18: 格式闸报告字段有界**
    - 生成超长 stderr 与多段出错片段，断言 `passed=false` 报告 `stderr_excerpt ≤2000`、每段 `excerpt ≤500` 且最多 10 段、含失败工具名与退出码
    - _Validates: Requirements 9.4_
  - [ ]* 18.4 编写 Property 19 属性测试
    - **Property 19: 格式闸超时判负并记录**
    - stub 工具运行超过 `format_gate_timeout`，断言 `passed=false`、进程被终止、报告记录超时工具名与所用阈值
    - _Validates: Requirements 9.7_
  - [ ]* 18.5 编写 Property 20 属性测试
    - **Property 20: 格式闸保留原始产物**
    - 生成 `passed=false` 判定，断言原始产物文件判定前后字节级一致且仍存在
    - _Validates: Requirements 9.9_

- [ ] 19. 实现 Format_Repair_Loop（新建 `export/format_repair.py`）
  - [ ] 19.1 实现 `Format_Repair_Loop.run(ws, report, exporter, gate) -> RepairOutcome`
    - 复用 `run_tool_loop` 模式驱动 LLM：把工具错误（≤2000）+ 出错 Markdown 片段（防御式截断 ≤8000）交给 LLM 产出修复后 Normalized_Markdown（Req 10.1）；经 `Content_Contract` 校验（Req 10.6），合规则仅经 `AgentResult.mutations` 写回对应章节 `content`（Req 10.5），重新导出并 `Format_Gate.check`（Req 10.2）；受 `max_repair_attempts` 约束使工具链运行总次数 ≤ `max_repair_attempts + 1`（Req 10.3/11.6）；首次 `passed=True` 即终止并采用（Req 10.4）；LLM 失败/不合规 → 丢弃、章节字节级不变、计入尝试次数（Req 10.6/10.7）；每次尝试记录工具错误类别与尝试序号、不打印密钥/完整请求体（Req 10.8）；终止状态 `∈ {repaired_within_limit, repair_exhausted}`（Req 11.1）
    - _Requirements: 10.1, 10.2, 10.3, 10.4, 10.5, 10.6, 10.7, 10.8, 11.1, 11.6_
  - [ ]* 19.2 编写 Property 21 属性测试
    - **Property 21: 修复循环终止性**
    - 生成 `max_repair_attempts ∈ [0,10]` 与任意格式闸失败序列，断言工具链运行总次数 ≤ `max+1`、循环必终止、终止状态取值合法、首次 `passed=true` 后即终止采用
    - _Validates: Requirements 10.3, 10.4, 11.1, 11.6_
  - [ ]* 19.3 编写 Property 22 属性测试
    - **Property 22: 修复输入防御式截断**
    - 生成超长工具错误与 Markdown 片段，断言交给 LLM 的工具错误 ≤2000、Markdown 片段 ≤8000，且外部工具/LLM 输出解析前截断至 ≤8000
    - _Validates: Requirements 10.1, 12.8_
  - [ ]* 19.4 编写 Property 23 属性测试
    - **Property 23: 无效修复被丢弃且章节不变**
    - stub LLM 产出不可解析/不合规输出，断言丢弃、不写回、目标章节 `content` 字节级不变、计入尝试次数
    - _Validates: Requirements 10.6_
  - [ ]* 19.5 编写 Property 24 属性测试
    - **Property 24: 修复可观测脱敏**
    - 生成修复尝试序列，断言每次尝试记录工具错误类别与尝试序号（1..max），不打印 API 密钥或完整请求体
    - _Validates: Requirements 10.8_
  - [ ]* 19.6 编写修复 LLM 失败路径单元测试
    - stub LLM 抛错/超时，断言丢弃该次、不写回、计入尝试次数（Req 10.7）
    - _Requirements: 10.7_

- [ ] 20. Checkpoint - 确保 Format_Gate 与 Format_Repair_Loop 测试通过
  - Ensure all tests pass, ask the user if questions arise.

- [ ] 21. 接入 Orchestrator._export_phase（`orchestrator.py`）
  - [ ] 21.1 在 `_export_phase` 注入可选 `format_gate` 与 `format_repair_loop`（依赖抽象，构造参数注入，Req 12.2）
    - 导出后运行 `Format_Gate.check`；`passed=false` → 运行 `Format_Repair_Loop.run`，其写回经 `WorkspaceRepository`（Req 12.1）；`repair_exhausted` → 不中止管线、输出最近一次产物（`max_repair_attempts==0` 时为原始产物）（Req 11.2）、在 `ExportResult` 与事件以一致措辞标注「格式未通过：已达修复上限」+ 最后错误片段 ≤2000（Req 11.3）、经既有可观测系统持久化诊断（Req 11.4）；多格式导出某格式失败不回滚其他已成功格式（Req 8.5）；工作区保留最后一次成功写回的章节 `content` 字节级不回滚（Req 11.5）；`exporter.export(ws, dir)` 签名不变（Req 6.8）；经既有可观测/用量记录事件与用量（Req 12.7）
    - _Requirements: 6.8, 8.5, 11.2, 11.3, 11.4, 11.5, 12.1, 12.2, 12.7_
  - [ ]* 21.2 编写 Property 25 属性测试
    - **Property 25: 修复耗尽优雅降级**
    - 生成以 `repair_exhausted` 终止的修复，断言管线不中止、输出最近产物、`ExportResult` 与事件一致标注「格式未通过：已达修复上限」+ 最后错误片段 ≤2000、持久化可定位诊断
    - _Validates: Requirements 11.2, 11.3, 11.4_
  - [ ]* 21.3 编写 Property 26 属性测试
    - **Property 26: pandoc 不可用时降级隔离且标注一致**
    - 生成多格式导出、pandoc 不可用且 `fallback`，断言管线不中止、可产出格式标注「已降级：pandoc 不可用」、不可产出格式以 1–500 字符错误标注失败且不回滚其他格式
    - _Validates: Requirements 8.2, 8.3, 8.5_
  - [ ]* 21.4 编写 Property 30 属性测试
    - **Property 30: 修复终止后产物与工作区一致**
    - 生成任意修复终止路径，断言工作区保留最后一次成功写回的章节 `content`、字节级不回滚、产物与工作区一致
    - _Validates: Requirements 11.5_
  - [ ]* 21.5 编写依赖倒置装配单元测试
    - 断言 Orchestrator 经注入获取 gate/repair 而非实例化具体类；`get_exporter` 返回实例且满足 `DocumentExporter` 协议、`export(ws, dir)` 签名不变
    - _Requirements: 6.8, 12.2_

- [ ] 22. 端到端单一写入路径与产物存在性总装校验
  - [ ] 22.1 串联补丁应用/整章重写/修复写回，确认全部写入 100% 经 `AgentResult.mutations` → `WorkspaceRepository`，且导出 `ExportResult.files` 所列路径均存在
    - 复核无任何绕过仓储的文件写入接口；写回仅涉及目标章节（Req 1.6/3.3/10.5/12.1）
    - _Requirements: 1.6, 3.3, 6.7, 10.5, 12.1_
  - [ ]* 22.2 编写 Property 28 属性测试
    - **Property 28: 单一原子写入路径**
    - 生成补丁应用/整章重写/修复写回场景，断言 100% 写入经 `AgentResult.mutations` → `WorkspaceRepository`、无绕过路径、写回仅涉及目标章节
    - _Validates: Requirements 1.6, 3.3, 10.5, 12.1_
  - [ ]* 22.3 编写 Property 16 属性测试
    - **Property 16: 导出产物路径均存在**
    - 生成成功导出，断言 `ExportResult.files` 每个路径在文件系统中实际存在
    - _Validates: Requirements 6.7_
  - [ ]* 22.4 编写 Property 29 属性测试
    - **Property 29: 落盘失败原子回滚**
    - stub 落盘失败，断言 `WorkspaceRepository` 回滚到写前字节级状态、不留部分写入；断点重启已完成章节 `content` 字节级不变且不重复落盘
    - _Validates: Requirements 12.3, 12.5_
  - [ ]* 22.5 编写 Property 12 属性测试
    - **Property 12: 数学定界符内部不被破坏**
    - 生成含 `$...$`/`$$...$$` 的内容，经 normalize + pandoc 管线（stub 转换保真）转换后断言数学定界符与内部符号保留为等价数学标记而非逐字符转义纯文本
    - _Validates: Requirements 5.4, 6.3_

- [ ] 23. 文档：pandoc 为外部系统二进制（`pyproject.toml` + README/说明）
  - [ ] 23.1 在 `pyproject.toml` 增加说明性注释/文档段，明确 pandoc（及可选 pdflatex）为外部系统可执行程序而非 pip 依赖，不新增 pip 依赖项，并附安装指引指向 Req 8.4 的错误信息
    - _Requirements: 6.1, 8.4_

- [ ] 24. 可选集成测试（真实 pandoc 环境，1–3 例，不做属性化）
  - [ ]* 24.1 编写 pandoc 可用环境集成测试
    - LaTeX/docx 由 pandoc 生成、数学正确保留、docx 含多结构元素而非单段落（Req 6.1/6.6）；可选 pdflatex 编译纳入格式闸判据（Req 9.2）；事件与 token 用量被既有系统记录（Req 4.5/12.7）
    - _Requirements: 6.1, 6.6, 9.2, 4.5, 12.7_

- [ ] 25. 最终 Checkpoint - 确保全部测试通过
  - Ensure all tests pass, ask the user if questions arise.

## Notes

- 标注 `*` 的子任务为可选测试任务（单元/属性/集成），可为快速 MVP 跳过；顶层任务与核心实现任务不加 `*`。
- 每个任务引用具体需求编号（`_Requirements: X.Y_`）以保证可追溯；每条属性测试显式引用设计文档中的 Property 编号与其验证的需求。
- 属性测试统一使用 Hypothesis、每条最少 100 次迭代（`@settings(max_examples=100)`），pandoc/pdflatex 与 LLM 一律以 stub/mock 注入。
- 契约保持：新增逻辑仅经 `AgentResult.mutations` → `WorkspaceRepository` 落盘；`Format_Gate` 全程不调用 LLM；依赖倒置（注入 gate/repair/exporter/LLM）；不重新设计 sibling venue-templates-figures-tables 组件。
- 各 Checkpoint 用于增量验证；导出器改造保留原手写渲染器作为 pandoc 不可用时的回退。

## Task Dependency Graph

```json
{
  "waves": [
    { "id": 0, "tasks": ["1.1", "2.1", "3.1", "23.1"] },
    { "id": 1, "tasks": ["1.2", "2.2", "3.2", "4.1", "5.1"] },
    { "id": 2, "tasks": ["1.3", "1.4", "4.2", "5.2", "7.1", "9.1", "18.1"] },
    { "id": 3, "tasks": ["4.3", "4.4", "4.5", "4.6", "7.2", "8.1", "10.1", "13.1", "14.1", "15.1"] },
    { "id": 4, "tasks": ["8.2", "8.3", "8.4", "8.5", "9.2", "10.2", "10.3", "11.1", "13.2", "14.2", "15.2", "15.3", "16.1", "18.2", "18.3", "18.4", "18.5"] },
    { "id": 5, "tasks": ["11.2", "16.2", "19.1"] },
    { "id": 6, "tasks": ["19.2", "19.3", "19.4", "19.5", "19.6", "21.1"] },
    { "id": 7, "tasks": ["21.2", "21.3", "21.4", "21.5", "22.1"] },
    { "id": 8, "tasks": ["22.2", "22.3", "22.4", "22.5", "24.1"] }
  ]
}
```
