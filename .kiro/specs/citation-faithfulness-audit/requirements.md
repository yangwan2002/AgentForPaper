# Requirements Document

需求文档：citation-faithfulness-audit（引用忠实性审计 · 声明级 grounded 引用校验）

## Introduction

本特性在现有学术论文写作多智能体系统（`src/paper_agent/`）之上，补齐 SOTA 差距分析识别出的**头号可被审稿人捕获的失败模式**：系统当前只核验引用「存在性」（`CitationVerifier` 按 DOI / arXiv / 标题回查）与「元数据 + 正文-文献对应」（`CitationAuditAgent`），但**明确不判定被引文献是否真正支撑其所挂靠的声明句**——`CitationAuditAgent` 的文档字符串已把「引用恰当性 ④」显式列为范围外。现代实践（检索-grounded 引用校验，如 CiteGuard / CiteME 风格）要求**声明级忠实性**：对正文中每一处引用，基于被引文献的**检索到的文本**判断该文献是否支撑这一具体表述，而**不依赖模型的参数化记忆**。

本特性新增一个 `Citation_Faithfulness_Audit`，在已生成/已验证内容之上运行，流程为：

1. **抽取声明-引用对**：对章节草稿中每个正文 `[id]` 标注（复用 `quality_gate.extract_text_citations` 的同一 ASCII-id 扫描），将其与所在的声明句关联，产出 `Claim_Citation_Pair`。
2. **组装 grounding 文本**：仅从**可用来源**为被引文献收集 `Grounding_Text`——`ReferenceEntry.title` + `abstract` + `abstract_sections`（复用 `paper_section_tool.extract_section`），并在 `pdf_url` / 结构化分段可用时可选取用某一段落；**绝不依赖模型记忆**。
3. **LLM-as-judge 判定**：给判定器**仅**提供「声明句 + 检索到的 grounding 文本」，产出每对的结构化裁决——枚举 `{supported, weak_support, unsupported, cannot_verify}` + 简短理由 + 可选支撑片段。解析**必须**经 `StructuredParser`（`ParseStatus` 为 `PARSED` / `MOCK_FALLBACK` / `FAILED`）；解析失败或 grounding 不足**绝不**捏造裁决，一律落到 `cannot_verify`（永不给出假的 `supported`）。
4. **持久化报告**：产出结构化 `Citation_Faithfulness_Report`（与 `citation_audit` / `quality_report` 同风格）落盘工作区，含每处引用的裁决与严重度：`unsupported` → high；`weak_support` → medium；`cannot_verify` → low/info。
5. **接入反馈闭环**：将无支撑引用作为**客观质量信号**暴露给写作/评审反馈闭环（类比 `Quality_Gate` 的 high 问题驱动 `gate_fixes`），使写作智能体可修正或删除该引用；并可导出/可见。定义其如何在**不破坏既有契约**的前提下驱动修订。

本特性严格沿用本代码库既有契约：智能体从不直接写工作区，只返回 `AgentResult.mutations` 经 `Workspace_Repository` 原子应用（唯一写入路径）；依赖倒置（`LLMProvider`、`RetrievalProvider`、`StructuredParser` 经注入）；仅针对**检索到的文本**判定，grounding 为空/不足时落 `cannot_verify`（禁止在无 grounding 时断言支撑）；不使用 `eval`/`exec`，外部文本视为不可信，对喂入 LLM 的 grounding 文本做防御式截断；优雅降级（LLM 不可用 / mock → 确定性非承诺行为 `cannot_verify`，绝不假 `supported`，且不中止管线）；对 LLM 调用做可观测性与用量统计，单次判定 token 预算有界（截断 grounding 文本）；必须复用而非重复实现（`quality_gate.extract_text_citations`、`paper_section_tool.extract_section`、`StructuredParser`、`ReferenceEntry` 字段）；可配置启用/停用与阈值，停用时行为不变。

### 范围边界（Out of Scope，明确不重复）

- **引用存在性、元数据准确性、正文-文献对应（悬空/冗余）**：由既有 `CitationAuditAgent` 与 `CitationVerifier` 拥有，本特性不重复实现，仅在其产出的 `verified_references` 之上做声明级忠实性判定。
- **引用真实性核验（DOI/arXiv/标题回查）**：由 `CitationVerifier` 拥有。
- **正文数字 grounding、体裁必备元素、占位/空章节检查**：由既有 `Quality_Gate` 拥有；本特性只新增「引用-声明忠实性」这一维度，不改动既有 `Quality_Gate` 判定路径。
- **主观评分 / 对抗式评审**：由 `ReviewRecord` / `AdversarialReviewRecord` 拥有。

## Glossary

- **Citation_Faithfulness_Audit**：引用忠实性审计，本特性新增的智能体/组件，在已验证内容之上做声明级 grounded 引用校验，经 `AgentResult.mutations` 产出 `Citation_Faithfulness_Report`。
- **Claim_Citation_Pair**：声明-引用对，一个 `(claim_sentence, cited_reference_id, section_id)` 三元组，表示正文某声明句挂靠了某个被引文献 id。
- **Claim_Sentence**：声明句，正文中包含某处 `[id]` 标注的、由句子边界切分得到的完整表述文本。
- **Cited_Reference_Id**：被引文献 id，正文 `[id]` 标注中提取的 ASCII 标识符（与 `quality_gate.extract_text_citations` 同一扫描规则）。
- **Grounding_Text**：grounding 文本，为某被引文献从**可用来源**（`ReferenceEntry.title` + `abstract` + `abstract_sections`，可选取用 `pdf_url` / 结构化分段的某段落）组装的、供判定器阅读的检索文本；不含模型记忆内容。
- **Faithfulness_Verdict**：忠实性裁决，取值于枚举 `{supported, weak_support, unsupported, cannot_verify}`，附简短理由（rationale）与可选支撑片段（supporting_snippet）。
- **Faithfulness_Judge**：忠实性判定器，基于注入的 `LLMProvider` 的 LLM-as-judge，仅接收「声明句 + grounding 文本」，经 `StructuredParser` 产出结构化 `Faithfulness_Verdict`。
- **Citation_Faithfulness_Report**：引用忠实性报告，落盘工作区的结构化发现列表（每项含 section_id、cited_reference_id、claim 摘要、verdict、severity、rationale、parse_status 等），与 `citation_audit` / `quality_report` 同风格。
- **CitationVerifier**：既有引用真实性核验器（`tools/citation.py`），按 source_id / 标题回查真实来源；本特性不改动其职责。
- **CitationAuditAgent**：既有引用审计智能体（`agents/citation_audit_agent.py`），做存在性 ①、元数据 ②、对应 ③ 检查，显式不做恰当性 ④；本特性补齐 ④ 的忠实性维度。
- **StructuredParser**：既有结构化输出解析器（`parsing/structured_parser.py`），产出 `ParseOutcome`（`status ∈ {PARSED, MOCK_FALLBACK, FAILED}`），`PARSED` 时 `data` 逐字来自 provider 返回。
- **ParseStatus**：解析来源状态枚举（`workspace/models.py`）：`PARSED` / `MOCK_FALLBACK` / `FAILED`。
- **PaperWorkspace**：论文工作区（`workspace/models.py`），系统单一真相源。
- **ReferenceEntry**：文献记录（`workspace/models.py`），含 `id`、`title`、`abstract`、`abstract_sections`、`pdf_url`、`verified` 等字段。
- **SectionDraft**：章节草稿（`workspace/models.py`），含 `content` 正文与 `cited_reference_ids`。
- **PaperSectionTool / extract_section**：按引用 id + 段落名取段落的只读工具（`tools/paper_section_tool.py`），本特性复用其 `extract_section` 组装 grounding 文本。
- **extract_text_citations**：`quality_gate` 中扫描正文 `[id]` 标注的函数，本特性复用其扫描规则做引用抽取。
- **Quality_Gate**：既有确定性质量闸（`tools/quality_gate.py`），其 high 问题驱动 `gate_fixes` 修订；本特性以类比方式接入反馈闭环。
- **Workspace_Repository**：工作区仓储，唯一原子落盘写入路径；智能体只经 `AgentResult.mutations` 写回。
- **AgentResult**：智能体产出（`agents/base.py`），含 `mutations`（更新意图）、`logs`、`payload`。
- **LLMProvider / RetrievalProvider**：注入的 LLM / 检索抽象接口（依赖倒置）。
- **Feedback_Loop**：写作/评审反馈闭环，既有机制中 `Quality_Gate` 的 high 问题驱动写作智能体做定位式修订（`gate_fixes`）。
- **Token_Budget**：单次忠实性判定喂入 LLM 的 grounding 文本字符/token 上限（有界预算，经截断保证）。

## Requirements

### Requirement 1: 声明-引用对抽取

**User Story:** 作为系统维护者，我希望审计能从章节草稿中准确抽取每处正文引用及其所在声明句，以便逐条判断被引文献是否支撑该表述。

#### Acceptance Criteria

1. WHEN `Citation_Faithfulness_Audit` 处理某个 `SectionDraft`，THE Citation_Faithfulness_Audit SHALL 使用与 `quality_gate.extract_text_citations` 相同的 ASCII-id 扫描规则识别正文中的每个 `[id]` 标注，不新增第二套引用扫描实现。
2. WHEN Citation_Faithfulness_Audit 识别到一个 `[id]` 标注，THE Citation_Faithfulness_Audit SHALL 将该标注与其所在的 `Claim_Sentence` 关联，产出一个含 `section_id`、`Claim_Sentence`、`Cited_Reference_Id` 的 `Claim_Citation_Pair`。
3. WHEN Citation_Faithfulness_Audit 确定某 `[id]` 标注所在的 `Claim_Sentence`，THE Citation_Faithfulness_Audit SHALL 依句子边界切分正文，使该 `Claim_Sentence` 为包含该标注字符位置的完整句子。
4. WHERE 同一 `Claim_Sentence` 含多个 `[id]` 标注，THE Citation_Faithfulness_Audit SHALL 为每个不同的 `Cited_Reference_Id` 分别产出一个 `Claim_Citation_Pair`。
5. IF 某 `Cited_Reference_Id` 不属于工作区 `verified_reference_ids()`，THEN THE Citation_Faithfulness_Audit SHALL 将该对标注为 `unverified_reference` 并纳入报告，且不为该对调用 `Faithfulness_Judge`。
6. IF 某 `SectionDraft` 的正文为空或不含任何 `[id]` 标注，THEN THE Citation_Faithfulness_Audit SHALL 不为该章节产出任何 `Claim_Citation_Pair`。

### Requirement 2: grounding 文本组装（仅取可用来源）

**User Story:** 作为系统维护者，我希望判定所依据的文本仅来自被引文献的真实可用来源，以便判定 grounded 于检索文本而非模型记忆。

#### Acceptance Criteria

1. WHEN Citation_Faithfulness_Audit 为某 `Claim_Citation_Pair` 组装 `Grounding_Text`，THE Citation_Faithfulness_Audit SHALL 仅从对应 `ReferenceEntry` 的 `title`、`abstract` 与 `abstract_sections` 取材，不引入被引文献之外的任何文本。
2. WHERE 对应 `ReferenceEntry` 提供了 `abstract_sections` 或可启发式切片的 `abstract`，THE Citation_Faithfulness_Audit SHALL 复用 `paper_section_tool.extract_section` 抽取段落作为 `Grounding_Text` 的一部分，不新增第二套段落抽取实现。
3. WHERE 对应 `ReferenceEntry` 的 `pdf_url` 或结构化分段可用，THE Citation_Faithfulness_Audit SHALL 可选取用其中一个段落纳入 `Grounding_Text`。
4. THE Citation_Faithfulness_Audit SHALL 不使用 `LLMProvider` 的参数化记忆作为 `Grounding_Text` 的来源。
5. IF 某 `Claim_Citation_Pair` 对应 `ReferenceEntry` 的 `title`、`abstract` 与 `abstract_sections` 组装后得到的 `Grounding_Text` 去除空白后为空或长度低于配置的最小 grounding 字符阈值，THEN THE Citation_Faithfulness_Audit SHALL 判定该对为 grounding 不足，直接赋予 `Faithfulness_Verdict` 值 `cannot_verify`，且不调用 `Faithfulness_Judge`。
6. WHEN Citation_Faithfulness_Audit 将 `Grounding_Text` 喂入 `Faithfulness_Judge`，THE Citation_Faithfulness_Audit SHALL 先将 `Grounding_Text` 截断至配置的 `Token_Budget` 上限字符数。

### Requirement 3: LLM-as-judge 忠实性判定与解析状态处理

**User Story:** 作为系统维护者，我希望判定器仅凭声明句与 grounding 文本给出结构化裁决，并对解析失败做安全处理，以便永不捏造「支撑」结论。

#### Acceptance Criteria

1. WHEN Citation_Faithfulness_Audit 判定某 grounding 充足的 `Claim_Citation_Pair`，THE Faithfulness_Judge SHALL 仅接收该对的 `Claim_Sentence` 与其 `Grounding_Text` 作为判定输入，不接收其它章节正文或模型记忆提示。
2. WHEN Faithfulness_Judge 解析 LLM 输出，THE Citation_Faithfulness_Audit SHALL 经 `StructuredParser.request_json` 完成解析，不自行解析 LLM 原始文本为裁决。
3. WHEN `StructuredParser` 返回 `ParseStatus` 为 `PARSED`，THE Citation_Faithfulness_Audit SHALL 采用其 `data` 中的 `Faithfulness_Verdict`，且该 verdict 值 SHALL 取自枚举 `{supported, weak_support, unsupported, cannot_verify}`；IF `data` 中的 verdict 值不属于该枚举，THEN THE Citation_Faithfulness_Audit SHALL 将该对判定为 `cannot_verify`。
4. IF `StructuredParser` 返回 `ParseStatus` 为 `FAILED`，THEN THE Citation_Faithfulness_Audit SHALL 将该对判定为 `cannot_verify`，并记录解析失败原因，且不将其判定为 `supported` 或 `weak_support`。
5. IF `StructuredParser` 返回 `ParseStatus` 为 `MOCK_FALLBACK`，THEN THE Citation_Faithfulness_Audit SHALL 将该对判定为 `cannot_verify`，且不将其判定为 `supported` 或 `weak_support`。
6. THE Citation_Faithfulness_Audit SHALL 不在缺少充足 `Grounding_Text` 或解析未成功的情况下将任一 `Claim_Citation_Pair` 判定为 `supported`。

### Requirement 4: 裁决枚举与严重度映射

**User Story:** 作为论文作者，我希望每处引用得到明确的裁决与严重度，以便优先处理最严重的无支撑引用。

#### Acceptance Criteria

1. THE Citation_Faithfulness_Audit SHALL 使每个 `Claim_Citation_Pair` 的 `Faithfulness_Verdict` 恰好取值于枚举 `{supported, weak_support, unsupported, cannot_verify}` 之一。
2. WHEN 某 `Claim_Citation_Pair` 的 verdict 为 `unsupported`，THE Citation_Faithfulness_Audit SHALL 赋予该发现严重度 `high`。
3. WHEN 某 `Claim_Citation_Pair` 的 verdict 为 `weak_support`，THE Citation_Faithfulness_Audit SHALL 赋予该发现严重度 `medium`。
4. WHEN 某 `Claim_Citation_Pair` 的 verdict 为 `cannot_verify`，THE Citation_Faithfulness_Audit SHALL 赋予该发现严重度取值于 `{low, info}`。
5. WHEN 某 `Claim_Citation_Pair` 的 verdict 为 `supported`，THE Citation_Faithfulness_Audit SHALL 不将该发现计为需修订的问题。
6. WHEN Faithfulness_Judge 产出 `unsupported` 或 `weak_support` 裁决，THE Citation_Faithfulness_Audit SHALL 在该发现中附带 LLM 给出的简短理由（rationale），并在存在时附带支撑片段（supporting_snippet）。

### Requirement 5: 持久化忠实性报告

**User Story:** 作为论文作者，我希望审计结果以结构化报告持久化到工作区，以便可导出、可见、可复查。

#### Acceptance Criteria

1. WHEN Citation_Faithfulness_Audit 完成一次运行，THE Citation_Faithfulness_Audit SHALL 产出一个 `Citation_Faithfulness_Report`，其中每个 `Claim_Citation_Pair` 对应一条发现，含 `section_id`、`Cited_Reference_Id`、`Claim_Sentence` 摘要、`verdict`、`severity`、`rationale` 与该判定的解析来源状态。
2. WHEN Citation_Faithfulness_Audit 写回 `Citation_Faithfulness_Report`，THE Citation_Faithfulness_Audit SHALL 仅经 `AgentResult.mutations` 通过 `Workspace_Repository` 原子落盘，不直接写工作区。
3. THE `Citation_Faithfulness_Report` SHALL 以可 JSON 序列化的结构存储于 `PaperWorkspace`，与既有 `citation_audit` / `quality_report` 的序列化风格一致，使其经既有工作区序列化路径可持久化与反序列化。
4. WHEN 某工作区不含 `Citation_Faithfulness_Report` 字段的旧版本 JSON 被反序列化，THE 系统 SHALL 使该字段回落到空列表，保持向后兼容且不失败。
5. WHEN Citation_Faithfulness_Audit 再次运行同一工作区，THE Citation_Faithfulness_Audit SHALL 以本次运行结果替换上一次的 `Citation_Faithfulness_Report`，不与旧发现重复累加。

### Requirement 6: 接入反馈闭环驱动修订

**User Story:** 作为论文作者，我希望无支撑引用能像质量闸的高危问题一样驱动写作智能体修正或删除该引用，以便最终稿不残留无支撑引用。

#### Acceptance Criteria

1. WHEN `Citation_Faithfulness_Report` 含 verdict 为 `unsupported` 的发现，THE Feedback_Loop SHALL 将该发现作为定位到 `section_id` 的高严重度修订项暴露给写作智能体，类比 `Quality_Gate` high 问题驱动 `gate_fixes` 的既有方式。
2. WHEN 写作智能体收到某 `unsupported` 引用修订项，THE 写作智能体 SHALL 仅经 `AgentResult.mutations` 通过 `Workspace_Repository` 修正或删除该引用，不直接写工作区。
3. WHERE 存在 verdict 为 `unsupported` 的发现，THE 系统 SHALL 不将该轮判定为「引用忠实性达标」。
4. THE Citation_Faithfulness_Audit 接入 Feedback_Loop 的方式 SHALL 不改变既有 `Quality_Gate`、`ReviewRecord`、`AdversarialReviewRecord` 的判定契约与既有达标判定路径。
5. WHERE 本特性被配置停用，THE 系统 SHALL 不向 Feedback_Loop 注入任何忠实性修订项，既有反馈闭环行为保持逐字节不变。

### Requirement 7: 优雅降级、可观测性、不可信数据与截断

**User Story:** 作为系统操作者，我希望审计在 LLM 不可用或数据异常时优雅降级且可追溯，以便系统始终可用且行为可审计。

#### Acceptance Criteria

1. IF `LLMProvider` 不可用、调用失败、超时或被识别为 Mock，THEN THE Citation_Faithfulness_Audit SHALL 将受影响的 `Claim_Citation_Pair` 判定为 `cannot_verify`，绝不产出假的 `supported`，且不中止管线。
2. WHEN Faithfulness_Judge 调用 `LLMProvider`，THE 系统 SHALL 经既有可观测性与用量统计系统记录该次调用与 token 用量。
3. THE Citation_Faithfulness_Audit SHALL 将 `ReferenceEntry` 的 `abstract`、`abstract_sections`、`pdf_url` 内容与正文声明句视为不可信数据，不对其执行 `eval`/`exec`。
4. WHEN Citation_Faithfulness_Audit 构造喂入 `Faithfulness_Judge` 的提示，THE Citation_Faithfulness_Audit SHALL 对 `Grounding_Text` 与 `Claim_Sentence` 施加取值于 `Token_Budget` 配置的字符长度上限的防御式截断。
5. THE 可观测记录 SHALL 不打印 API 密钥或完整请求体，并对记录的文本片段施加长度上限。
6. IF 单个 `Claim_Citation_Pair` 的判定过程抛出异常，THEN THE Citation_Faithfulness_Audit SHALL 将该对记为 `cannot_verify`、记录失败原因，并继续处理其余对，不中止整次审计。

### Requirement 8: 可配置启用/停用与阈值

**User Story:** 作为系统操作者，我希望能配置审计的开关与阈值，以便按部署需要控制成本与严格度。

#### Acceptance Criteria

1. WHERE 配置将 `Citation_Faithfulness_Audit` 设为停用，THE 系统 SHALL 不抽取 `Claim_Citation_Pair`、不调用 `Faithfulness_Judge`、不写回 `Citation_Faithfulness_Report`，且系统其余行为逐字节不变。
2. WHERE 配置将 `Citation_Faithfulness_Audit` 设为启用，THE 系统 SHALL 按 Requirement 1 至 7 运行审计。
3. THE 系统 SHALL 支持配置最小 grounding 字符阈值与单次判定的 `Token_Budget` 上限，并在运行时采用所配置的取值。
4. IF 配置提供的阈值取值非法（如负数或非数值），THEN THE 系统 SHALL 采用文档化的默认阈值并记录该回退。

### Requirement 9: 契约保持与依赖倒置

**User Story:** 作为系统架构师，我希望本特性遵循既有契约，以便保持单一写入路径、依赖倒置与既有组件的复用。

#### Acceptance Criteria

1. THE Citation_Faithfulness_Audit SHALL 仅经 `AgentResult.mutations` 通过 `Workspace_Repository` 落盘，不调用绕过 `Workspace_Repository` 的工作区写入接口。
2. THE Citation_Faithfulness_Audit SHALL 依赖注入的 `LLMProvider`、`RetrievalProvider` 与 `StructuredParser` 抽象，不在其内部实例化具体 provider 或解析器实现类。
3. THE Citation_Faithfulness_Audit SHALL 复用 `quality_gate.extract_text_citations`、`paper_section_tool.extract_section`、`StructuredParser` 与 `ReferenceEntry` 的既有字段，不重复实现引用扫描、段落抽取与结构化解析逻辑。
4. THE 本特性 SHALL 不修改 `CitationVerifier`、`CitationAuditAgent`、`Quality_Gate` 的既有职责与判定路径，仅新增声明级忠实性维度。
5. WHEN 管线中断后重启，THE Workspace_Repository SHALL 从最近一次成功提交状态恢复，使已落盘的 `Citation_Faithfulness_Report` 逐字节比对不变且不重复落盘。
