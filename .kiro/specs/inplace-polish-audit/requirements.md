# Requirements Document

## Introduction

当用户提供一份成品初稿（.docx/.tex）并要求「保留原格式润色」时，系统当前只走保结构
润色路径（`InplacePolishWorkflow` / `polish_docx_inplace` / `polish_latex_inplace`），
该路径**只保护、不评审**：它保证润色不改动引用与数字、不破坏格式，但**不核验**初稿里
已有参考文献是否真实存在、也不核验引言等处的引用是否真的支撑了论断。

本特性（方案 B：评审只读旁路）在**不改动保格式润色产物、也不走脆弱的「文本往返回写」**
的前提下，给保格式润色**并行挂一条只读审计**：把原稿文本抽进一个**临时、隔离**的审计
工作区，复用既有的「参考文献真实性核验」（`verify_existing_references` 背后的
`CitationParser` + `CitationVerifier`）与「引用忠实性核验」（`CitationFaithfulnessAgent`
+ `FaithfulnessJudge`），产出一份**建议性问题清单**随润色结果一并返回。

审计是**只读**的：它只生成报告，绝不驱动对润色产物的字节级改写；审计的任何失败/降级都
不影响保格式润色产物本身。这样用户一次交互即可同时得到：保原格式的润色稿 + 文献真伪与
引用忠实性的评审意见。

## Glossary

- **Inplace_Polish（保格式润色）**：`InplacePolishWorkflow` 对用户原 .docx/.tex 的保结构
  语言润色，产出新文件、原稿只读、格式逐字保留。
- **Audit（只读审计）**：对原稿内容做的只读评审，含参考文献真实性核验与引用忠实性核验，
  产出 `Audit_Report`，不修改任何产物。
- **Audit_Workspace（审计工作区）**：为审计临时构造的 `PaperWorkspace`，与用户真实工作区
  及润色产物完全隔离；审计结束即弃，绝不落盘到用户工作区。
- **Reference_Authenticity（参考文献真实性）**：初稿参考文献表中的条目是否在检索库中真实
  存在（按标题/DOI 回查），由 `CitationVerifier` 判定。
- **Citation_Faithfulness（引用忠实性）**：正文某处引用的文献是否真的支撑该处论断，由
  `CitationFaithfulnessAgent` 基于「标题+摘要」级 grounding 判定，产出四类裁决
  （supported / weak_support / unsupported / cannot_verify）。
- **Audit_Report（审计报告）**：审计产出的结构化发现集合 + 人可读问题清单。
- **Retrieval_Available（检索可用）**：检索 provider 为真实来源（非 mock）且可访问。

## Requirements

### Requirement 1: 保格式润色并行只读审计

**User Story:** 作为投稿前的作者，我希望在对我的初稿做保格式润色的同时，系统顺便帮我核验
已有参考文献的真伪与正文引用是否忠实，这样我一次就能拿到润色稿和评审意见。

#### Acceptance Criteria

1. WHEN 用户提供 .docx/.tex 原稿并触发 Inplace_Polish 且审计开关开启 THE 系统 SHALL 在
   产出保格式润色文件后，对同一原稿运行一次只读 Audit，并把 Audit_Report 随润色结果一并返回。
2. WHERE 审计开关关闭 THE 系统 SHALL 只执行 Inplace_Polish，不运行 Audit，行为与现状逐字节一致。
3. WHEN Audit 运行 THE 系统 SHALL 不修改 Inplace_Polish 产出的文件内容。
4. IF 原稿既无可解析的参考文献、也无正文引用标注 THEN 系统 SHALL 产出「无可核验引用」的
   Audit_Report，而非报错或静默跳过。

### Requirement 2: 审计的只读隔离

**User Story:** 作为用户，我不希望"顺便做的审计"污染我的工作区或改坏我的润色稿。

#### Acceptance Criteria

1. WHEN Audit 需要把原稿抽成结构化内容 THE 系统 SHALL 使用一个临时的 Audit_Workspace，
   不写入用户的真实工作区（`repo`）。
2. WHEN Audit 完成 THE 系统 SHALL 不将 Audit_Workspace 的任何改动持久化到用户工作区。
3. WHILE Audit 运行 THE 系统 SHALL 不经内容护栏改写、不产生对用户工作区 `section_drafts`
   的任何写入。
4. WHEN Audit 向检索库核验或调用判定 LLM THE 系统 SHALL 仅读取原稿自身文本与被引文献的
   元数据/摘要，不改动原稿文件。

### Requirement 3: 参考文献真实性核验

**User Story:** 作为作者，我想知道初稿里列的参考文献有没有编造或写错的。

#### Acceptance Criteria

1. WHEN Audit 运行且原稿含参考文献表 THE 系统 SHALL 用 `CitationParser` 解析条目，并对每条
   用 `CitationVerifier` 按标题/DOI 回查真实性。
2. WHEN 某条参考文献在检索库中查到高相似匹配 THE 系统 SHALL 标注其为「真实」，并在年份不一致
   时附「年份可能有误」的提示。
3. WHEN 某条参考文献查不到匹配 THE 系统 SHALL 标注其为「未核验/疑似不存在」，不臆断为真。
4. IF Retrieval_Available 为假 THEN 系统 SHALL 把真实性结论标注为「检索不可用，无法核验」，
   不静默判为真、也不判为假。

### Requirement 4: 引用忠实性核验

**User Story:** 作为作者，我想知道我在引言里引的文献是不是真的支撑了我写的那句话。

#### Acceptance Criteria

1. WHEN Audit 运行 THE 系统 SHALL 复用 `CitationFaithfulnessAgent` 对「已核验为真实的引用」
   逐条做声明级忠实性判定。
2. WHEN 被引文献的 grounding（标题+摘要级）不足以判定 THE 系统 SHALL 裁决为 cannot_verify，
   不臆断为 supported。
3. WHEN 某引用被裁决为 unsupported 或 weak_support THE 系统 SHALL 在 Audit_Report 中按严重度
   列出该处的章节、论断摘录、被引文献与裁决理由。
4. WHERE 引用指向的文献未通过真实性核验 THE 系统 SHALL 将该引用标注为 cannot_verify（因无
   可信 grounding），不调用判定 LLM 去"假装"核验。

### Requirement 5: 审计报告

**User Story:** 作为用户，我希望审计结果是一份我能直接看懂、能据以修改的问题清单。

#### Acceptance Criteria

1. WHEN Audit 完成 THE 系统 SHALL 产出结构化 Audit_Report，含：参考文献真实性统计（总数/
   真实/未核验）与忠实性发现列表（章节、引用、裁决、严重度、理由摘录）。
2. WHEN 把 Audit_Report 呈现给用户 THE 系统 SHALL 渲染为简洁的人可读问题清单，且标明这是
   「建议」而非对产物的强制改动。
3. WHEN Audit_Report 无任何问题 THE 系统 SHALL 明确告知「参考文献均可核验、引用未发现明显
   不支撑」，而非空白输出。
4. WHERE 报告含文本摘录 THE 系统 SHALL 对摘录施加长度上限（脱敏，防止把整段正文回灌）。

### Requirement 6: 诚实降级与故障隔离

**User Story:** 作为用户，我不希望审计出问题时连累我的润色稿，也不希望它假装核验成功。

#### Acceptance Criteria

1. IF Audit 过程中任一步抛异常 THEN 系统 SHALL 捕获该异常、在报告中如实标注该步未完成，并
   仍正常返回 Inplace_Polish 产物。
2. IF 原稿无法解析出章节或参考文献 THEN 系统 SHALL 在报告中说明「无法解析，未能审计」，不报错中断。
3. WHEN 检索或判定 LLM 触达预算/超时 THE 系统 SHALL 停止进一步核验、在报告中标注「部分未核验」，
   不无界重试。
4. WHERE 审计结果为「未通过/未核验」THE 系统 SHALL 不因此阻断或撤销 Inplace_Polish 产物的交付。

### Requirement 7: 装配开关与向后兼容

**User Story:** 作为集成方，我希望这条旁路可配置，且默认不破坏既有行为。

#### Acceptance Criteria

1. WHERE 提供了审计开关配置 THE 系统 SHALL 依配置决定 Inplace_Polish 是否附带 Audit。
2. WHERE 未装配检索/判定依赖（如 mock provider）THE 系统 SHALL 安全降级为「不可核验」报告，
   不崩溃、不阻断润色。
3. WHEN 审计开关关闭 THE 系统 SHALL 使 `InplacePolishWorkflow` 的产物与调用行为与本特性引入
   前逐字节一致。
