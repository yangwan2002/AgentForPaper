# Requirements Document

## Introduction

用户给一份**成品初稿**（.docx/.tex），要求「补写引言、在文末加参考文献并导出，同时保留原格式」。
当前系统对这种「**在原稿上增补新章节**」的复合任务走的是 `import_draft`（把原稿拍平成文本）→
改工作区 `section_drafts` → `export_paper`（从文本重建）这条链路，导致：

- **公式丢失**：docx 的公式存在 OMML（`m:oMath`）里、不在 `paragraph.text`，导入即丢；.tex 的
  `\begin{equation}` 等在拍平时丢。
- **参考文献重复**：手建的「参考文献」章节 + 导出器自动再生成一份 → 两份叠加。
- **重复标题**：章节标题 + 正文里又带一个同名标题 → 渲染两个。
- **表格失真**：原表格被拍平重画，booktabs/列宽等精细排版丢失。

本特性（方案 C：原稿就地增补）提供一条**保结构的"增补"路径**：**直接在用户原文件上插入**新章节
（如引言）与参考文献，**从不 re-emit 原有内容**，因此原稿的公式、表格、字体、编号、页眉页脚等
一切格式**逐字保留**；参考文献只有插入的**唯一一份**、套学术排版（悬挂缩进+单倍行距）。这是对
既有 `polish_docx_inplace` / `polish_latex_inplace`「只改不加」保结构范式的自然扩展——从「只改」
扩展到「能加新章节」，且同样**只增不毁**。

## Glossary

- **Augment（就地增补）**：在用户原文件（.docx/.tex）上**插入**新章节 / 追加参考文献，产出新文件，
  原稿只读，原有内容逐字保留。
- **Additive_Only（只增不改）**：增补操作只新增段落/元素，**绝不重写或删除**原有段落、表格、公式；
  原稿既有内容在产物中原样存在。
- **Insertion_Point（插入点）**：新章节插入的位置（docx：文档开头 / 指定锚点前；tex：首个
  `\section` 前 / `\end{document}` 前）。
- **Preservation_Check（无损校验）**：产物必须**包含**原稿的全部原有结构元素（段落数、表格、公式、
  图形计数只增不减；原有标题集合为产物标题集合的子集），否则判失败并保留原稿。
- **Reference_Block（参考文献块）**：插入的唯一一份参考文献列表（docx：受保护样式段落 + 悬挂缩进 +
  单倍行距；tex：`thebibliography` 或等价）。

## Requirements

### Requirement 1: docx 就地插入新章节（保原内容/公式/表格/格式）

**User Story:** 作为作者，我想在我的 Word 初稿上补一段引言，且原来的公式、表格、排版一个都不能丢。

#### Acceptance Criteria

1. WHEN 用户提供 .docx 原稿并要求新增一个章节（标题 + 正文）THE 系统 SHALL 打开原 docx、在
   Insertion_Point 处插入该章节的标题段落与正文段落，产出新 docx。
2. WHEN 插入新章节 THE 系统 SHALL 为 Additive_Only：不重写、不删除任何原有段落/表格/公式/图形。
3. WHEN 产出新 docx THE 系统 SHALL 使原稿的公式（OMML）、表格、字体/样式/编号/页眉页脚**逐字保留**。
4. WHERE 指定了插入位置（开头 / 某锚点前）THE 系统 SHALL 按该位置插入；未指定时默认插入到正文开头。
5. WHEN 增补完成 THE 系统 SHALL 写出到独立新文件，原稿输入文件字节不变。

### Requirement 2: docx 就地追加参考文献（唯一一份，学术排版）

**User Story:** 作为作者，我想在 Word 初稿文末加一份参考文献，单倍行距、悬挂缩进，且不要出现两份。

#### Acceptance Criteria

1. WHEN 用户要求追加参考文献 THE 系统 SHALL 在原 docx 文末插入**唯一一份** Reference_Block（标题 +
   逐条文献段落）。
2. WHEN 渲染参考文献段落 THE 系统 SHALL 套用悬挂缩进 + 单倍行距（复用 `format_reference_paragraph`）。
3. WHEN 走就地增补路径 THE 系统 SHALL 不触发导出器的「自动再生成参考文献」，即产物中参考文献只有一份。
4. IF 原 docx 已存在参考文献标题 THEN 系统 SHALL 不再重复插入一个同名标题（去重）。

### Requirement 3: latex 就地插入新章节与参考文献（保 preamble/公式/宏）

**User Story:** 作为作者，我想在我的 .tex 初稿里补引言和参考文献，preamble、宏、公式一律不动。

#### Acceptance Criteria

1. WHEN 用户提供 .tex 原稿并要求新增章节 THE 系统 SHALL 在首个 `\section` 前（或指定锚点）插入
   `\section{...}` 与其正文，其余源码逐字节保留。
2. WHEN 用户要求追加参考文献 THE 系统 SHALL 在 `\end{document}` 前插入唯一一份参考文献块。
3. WHEN 增补 .tex THE 系统 SHALL 逐字保留 preamble（至 `\begin{document}`）、`\newcommand` 宏、
   行内/行间数学、环境、`\cite`/`\ref`、注释与整体结构。
4. WHEN 增补完成 THE 系统 SHALL 写出到独立新 .tex，原稿输入文件字节不变。

### Requirement 4: 原有内容无损校验

**User Story:** 作为用户，我要确信"增补"绝不会偷偷改坏或丢掉我原稿的任何内容。

#### Acceptance Criteria

1. WHEN 增补 docx 完成 THE 系统 SHALL 做 Preservation_Check：产物的段落/表格/公式/图形计数**不少于**
   原稿，且原稿的标题集合是产物标题集合的子集。
2. IF Preservation_Check 失败（检出原有内容被改动/丢失）THEN 系统 SHALL 判该次增补失败、保留原稿、
   不产出可能破坏的文件，并诚实上报原因。
3. WHEN 增补 .tex 完成 THE 系统 SHALL 校验原稿源码作为**连续子串**保留在产物中（只在插入点新增），
   否则判失败并保留原稿。
4. WHERE 任一增补步骤失败 THE 系统 SHALL 不修改用户原稿输入文件。

### Requirement 5: 正确选路（避免再走拍平重建）

**User Story:** 作为用户，当我给的是成品稿且要保格式，我不希望系统又去拍平重建把公式弄丢。

#### Acceptance Criteria

1. WHERE 源文件为 .docx/.tex 且诉求为「保留原格式增补内容」THE 系统 SHALL 使用就地增补能力，
   而非 `import_draft` + `add_section` + `export_paper` 的重建路径。
2. WHEN 就地增补工具可用 THE 系统 SHALL 以工具描述与系统提示引导 Agent 优先选用它做保格式增补。
3. WHEN 新章节正文由 LLM 撰写 THE 系统 SHALL 允许其为纯文本/Markdown（新内容通常无需保留原公式），
   但**原稿既有内容**始终经 Additive_Only 保留。

### Requirement 6: 故障隔离与向后兼容

**User Story:** 作为集成方，我希望这条能力加法式接入、失败不连累、默认不破坏既有行为。

#### Acceptance Criteria

1. IF 增补过程抛异常 THEN 系统 SHALL 捕获并作为工具失败诚实回灌，不崩溃、不破坏原稿。
2. IF 找不到 Insertion_Point（如 .tex 无 `\end{document}`）THEN 系统 SHALL 采用安全回退（追加到末尾）
   或明确上报，不静默丢内容。
3. WHERE 未调用就地增补能力 THE 系统 SHALL 使既有导出/润色/重建路径行为逐字节不变。
4. WHERE python-docx 不可用 THE 系统 SHALL 对 docx 增补给出可诊断错误，不产出半损坏文件。
