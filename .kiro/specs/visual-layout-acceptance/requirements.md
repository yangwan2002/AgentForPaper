# Requirements Document

## Introduction

系统会把学术论文编辑/转换为 `.docx`（格式转换、宽表跨栏、图片摆放、排版）。一次编辑之后，当前有**两道**把关：

1. **Preservation_Check（结构保留校验）**：只核对「什么都没丢」——段落 / 表格 / 图形（drawing）/ 脚注计数只增不减、原标题子集保留。它**不**判断版面看起来是否正确。
2. **文本级验收闸（Text_Acceptance_Gate）**：只做乱码 / 引用闭合 / 数量年限等**可机械检查**的文本信号（见 `agent_platform/acceptance.py`）。

结果是：**没有任何一道关卡真正「看」渲染出来的页面**，来判断版面是否符合用户诉求（例如「图放在页顶跨双栏、上方无大片空白」「正文分两栏」「表格未被逐字符换行」）。这个缺口造成过真实故障：某次智能体声称它做了版面改动，产物通过了 Preservation_Check，但页面依旧是错的（图上方一大片空白），智能体甚至**谎报成功**。

本特性新增一道**视觉验收闸 + 有界重编辑循环（Visual_Acceptance_Gate + Bounded_Re_Edit_Loop）**：当一次 docx 编辑通过 Preservation_Check 之后，系统可选地——

- 把产出的 `.docx` **渲染为逐页图片**（后端优先用 Windows 上的 Word COM 自动化：不可见地启动 Word、`ExportAsFixedFormat` 导出 PDF，保真度即 Word 自身的渲染；Word 不可用时回退 LibreOffice headless `--convert-to pdf`；随后用 PyMuPDF 按可配置 DPI 把 PDF 转为逐页 PNG）；
- **派生一个子智能体（Visual_Judge_SubAgent）**，用**多模态（具备视觉能力）LLM** 观察页面图片 + 用户陈述的版面诉求，判断版面是否匹配（yes/no + 具体缺陷，如「图 1 上方大片空白」「正文塌缩为单栏」）；
- 若不满足，把视觉批注反馈给编辑智能体重试，**限定在很小的轮数内**；若达到轮数上限仍不满足，**如实向用户上报**（绝不谎报成功）。

**核心边界（与用户讨论并达成一致）**：视觉判断是**建议性 + 有界重试**，**不是绝对硬闸**——视觉模型对**粗粒度**版面判断（大片空白、单栏 vs 双栏、图满栏 vs 单栏宽）可靠，但对像素级精确判断不可靠且可能幻觉。渲染保真度有告警：LibreOffice 的渲染 ≠ Microsoft Word，尤其在浮动体 / 分栏 / 分页处，故经 LibreOffice「通过」不等于在用户的 Word 里也对；Word COM 渲染即 Word 自身输出（无保真差），在 Windows 上必须优先。当前 LLM provider 抽象是**纯文本**的（`Message.content` 为字符串，`providers/llm` 无图像支持），支持多模态是本特性的**前置条件**。整套特性**默认关闭、可配置开启**，且在任何依赖缺失时**优雅降级**、回退既有行为，绝不阻断或拖垮主流程；同时**绝不触碰正确性核心**（引用 / 内容 / 忠实性）。

## Glossary

- **Visual_Acceptance_Gate（视觉验收闸）**：本特性新增的把关环节——在 docx 编辑通过 Preservation_Check 后，渲染页面并用视觉模型判断版面是否符合用户诉求，据此驱动有界重编辑或诚实上报。
- **Layout_Requirement（版面诉求）**：用户对版面的自然语言诉求（如「图放页顶跨双栏、上方无大空白」「正文两栏」「表格不逐字符换行」），是视觉判断的比对目标。
- **Render_Backend（渲染后端）**：把 `.docx` 渲染为 PDF 的可插拔后端。包含 **Word_COM_Backend**（Windows 上经 COM 自动化调用 Word `ExportAsFixedFormat`）与 **LibreOffice_Backend**（headless `--convert-to pdf`）。
- **Word_COM_Backend**：在 Windows 上以不可见方式驱动 Microsoft Word 导出 PDF 的渲染后端；其保真度等同 Word 自身渲染（无保真差），为**优先后端**。
- **LibreOffice_Backend**：以 LibreOffice headless 模式转 PDF 的**回退后端**；其渲染可能与 Microsoft Word 存在差异，尤其在浮动体 / 分栏 / 分页处。
- **Page_Rasterizer（逐页栅格化器）**：用 PyMuPDF 把 PDF 按可配置 DPI 转为逐页 PNG 图片的组件。
- **Page_Images（页面图片）**：渲染并栅格化后得到的逐页 PNG 图片集合，作为视觉判断的输入。
- **Multimodal_LLM（多模态模型）**：具备视觉能力、可同时接收文本与图像输入的 LLM；其模型 / 端点**独立配置**，可与主文本 LLM 不同。
- **Multimodal_Message_Path（多模态消息通路）**：对既有纯文本 `Message` / provider 通路的扩展，使消息能携带图像输入。当前 `Message.content` 为字符串、`providers/llm` 无图像支持，故此为本特性前置条件。
- **Visual_Judge_SubAgent（视觉判断子智能体）**：经既有子智能体机制（`agent_platform/subagents.py`）派生、以独立对话上下文运行的子智能体，用 Multimodal_LLM 观察 Page_Images + Layout_Requirement 并产出 Visual_Verdict。
- **Visual_Verdict（视觉裁定）**：视觉判断的结构化结果——是否满足（satisfied: yes/no）+ 具体缺陷清单（如「图 1 上方大片空白」「正文塌缩为单栏」）+ 建议性说明。
- **Bounded_Re_Edit_Loop（有界重编辑循环）**：把 Visual_Verdict 的缺陷反馈给编辑智能体重试的循环，轮数限定在很小的上限内，保证有限步终止。
- **Changed_Page_Selection（变化页选择）**：对编辑前 / 后两版渲染结果做逐页图像比对，挑出真正发生视觉变化的页面（含少量邻页上下文）只送视觉判断的策略；无编辑前基线时回退为受页数上限约束的页面预算。目的是聚焦判断、节省 VLM 成本，取代「固定送前 N 页」。
- **Layout_Affecting_Operation（版面相关操作）**：一轮任务中会产生版面后果、因而值得视觉校验的操作类别（如图跨栏、分栏、宽表适配、字体 / 字号设置、跨格式转 docx）；据此**确定性**决定是否自动触发 Visual_Acceptance_Gate，与之相对的是纯语言润色 / 加引用等无版面后果的操作。
- **Preservation_Check**：既有 docx 结构无损校验（`inplace_augment._preservation_check_docx`）——段落 / 表格 / 图形 / 脚注计数只增不减、原标题子集保留。
- **Correctness_Core（正确性核心）**：引用真伪、内容护栏、忠实性审计等必须受控的能力。本特性绝不触碰。
- **Graceful_Degradation（优雅降级）**：既有项目契约——依赖缺失 / 失败时干净跳过、回退既有行为，不阻断、不崩溃。

## Requirements

### Requirement 1: 把产出的 docx 渲染为逐页图片（Word COM 优先，LibreOffice 回退）

**User Story:** 作为对版面负责的用户，我希望系统能把编辑后的 docx 真实渲染成页面图片，以便有依据地判断版面是否正确。

#### Acceptance Criteria

1. WHEN Visual_Acceptance_Gate 需要评估一个 `.docx` 产物 THE Render_Backend SHALL 把该 `.docx` 渲染为 PDF，随后 THE Page_Rasterizer SHALL 用 PyMuPDF 把该 PDF 按配置 DPI 转为逐页 PNG。
2. WHERE 运行环境为 Windows 且 Microsoft Word 可用 THE 系统 SHALL 选择 Word_COM_Backend 作为渲染后端，以不可见方式驱动 Word 经 `ExportAsFixedFormat` 导出 PDF。
3. IF Word_COM_Backend 不可用 THEN THE 系统 SHALL 回退到 LibreOffice_Backend，以 headless `--convert-to pdf` 导出 PDF。
4. THE Page_Rasterizer SHALL 以可配置的 DPI 栅格化页面，未配置时使用文档化的默认 DPI。
5. WHEN 渲染与栅格化成功 THE 系统 SHALL 返回逐页 PNG 图片路径清单，顺序与文档页序一致。
6. WHEN 使用 LibreOffice_Backend 产出裁定 THE 系统 SHALL 在结果中附带保真度告警，说明 LibreOffice 渲染可能与 Microsoft Word 存在差异（尤其在浮动体 / 分栏 / 分页处）。
7. WHERE 存在编辑前的 docx 基线（如 docx→docx 就地编辑，或重编辑循环的上一轮产物）THE 系统 SHALL 对编辑前 / 后两版**分别渲染并逐页图像比对**，只把发生变化的页面（含少量邻页上下文）交 Visual_Judge_SubAgent，而非固定送前若干页。
8. IF 不存在编辑前的 docx 基线（如全新 `tex→docx` 转换）THEN THE 系统 SHALL 回退为受**配置页数上限**约束的页面预算。
9. WHEN 送往 Visual_Judge_SubAgent 的页面数超过配置的页数上限 THE 系统 SHALL 截断为上限内的最相关页面，并在裁定中标注「仅采样部分页面」。

### Requirement 2: 多模态消息通路作为前置条件

**User Story:** 作为开发者，我需要让消息与 provider 通路能携带图像，才能让视觉模型看到页面。

#### Acceptance Criteria

1. THE Multimodal_Message_Path SHALL 扩展既有消息 / provider 通路，使一条消息能同时携带文本与一张或多张图像输入。
2. WHERE 已配置 Multimodal_LLM 的模型 / 端点 THE 系统 SHALL 允许该配置独立于主文本 LLM 的 provider / 模型 / 端点。
3. THE Multimodal_Message_Path SHALL 保持既有纯文本消息通路的行为不变，使未使用图像输入的既有调用逐字节向后兼容。
4. IF 未配置 Multimodal_LLM THEN THE 系统 SHALL 跳过 Visual_Acceptance_Gate 并回退既有行为（见 Requirement 6）。

### Requirement 3: 视觉判断子智能体

**User Story:** 作为用户，我希望有一个「看图的评审」来判断渲染出来的版面是否符合我提出的版面诉求。

#### Acceptance Criteria

1. WHEN Page_Images 与 Layout_Requirement 均就绪 THE 系统 SHALL 经既有子智能体机制派生 Visual_Judge_SubAgent，以独立对话上下文运行。
2. WHEN Visual_Judge_SubAgent 运行 THE 系统 SHALL 向 Multimodal_LLM 提供 Page_Images 与 Layout_Requirement，并要求其产出 Visual_Verdict（satisfied: yes/no + 具体缺陷清单 + 建议性说明）。
3. THE Visual_Judge_SubAgent SHALL 聚焦于粗粒度版面判断（如大片空白、单栏 vs 双栏、图满栏 vs 单栏宽），不做像素级精确判断。
4. THE Visual_Judge_SubAgent SHALL 以只读方式使用 Page_Images 与工作区产物，不修改产物内容。

### Requirement 4: 有界重编辑循环

**User Story:** 作为用户，当版面不对时我希望系统自动尝试修正，但不要无休止地烧钱烧时间。

#### Acceptance Criteria

1. IF Visual_Verdict 判定 not satisfied 且未达配置的最大轮数 THEN THE 系统 SHALL 把缺陷清单反馈给编辑智能体重试，并对重试产物重新渲染与判断。
2. THE Bounded_Re_Edit_Loop SHALL 将重试轮数限定在配置的最大轮数内，保证有限步终止。
3. WHEN Visual_Verdict 判定 satisfied THE 系统 SHALL 结束循环并放行产物。
4. WHEN 重试产物经渲染判断后仍未满足且已达最大轮数 THE 系统 SHALL 结束循环并进入诚实上报（见 Requirement 7）。
5. WHEN 重编辑重试执行 THE 系统 SHALL 使编辑动作仍经既有写工具 / 护栏 / 单一写路径，不另立写路径、不绕过既有有界性。

### Requirement 5: 建议性而非硬闸的语义

**User Story:** 作为用户，我理解视觉模型会看错，所以它的判断应是建议，不应因为它的误判而卡死我的产物。

#### Acceptance Criteria

1. THE Visual_Acceptance_Gate SHALL 将 Visual_Verdict 作为建议性信号处理，不作为阻断交付的绝对硬闸。
2. WHEN Bounded_Re_Edit_Loop 达到最大轮数仍未满足 THE 系统 SHALL 交付最后一版产物并附带未满足的版面缺陷说明，而非阻断交付。
3. THE 系统 SHALL 在上报中区分「视觉模型建议性判断」与「Preservation_Check / 文本级验收的确定性判断」，不使前者凌驾于后者。

### Requirement 6: 在每一处依赖缺失时优雅降级

**User Story:** 作为用户，我不希望缺少 Word / LibreOffice / 多模态模型，或渲染视觉调用出错时，整个主流程被拖垮。

#### Acceptance Criteria

1. IF 无任何可用 Render_Backend（Word 与 LibreOffice 均不可用）THEN THE 系统 SHALL 跳过 Visual_Acceptance_Gate 并回退既有行为，不阻断、不崩溃。
2. IF 未配置 Multimodal_LLM THEN THE 系统 SHALL 跳过 Visual_Acceptance_Gate 并回退既有行为。
3. IF 渲染、栅格化或视觉调用失败 THEN THE 系统 SHALL 隔离该失败、跳过 Visual_Acceptance_Gate 并回退既有行为，并如实记录跳过原因。
4. WHEN Visual_Acceptance_Gate 被跳过 THE 系统 SHALL 保持既有 Preservation_Check 与文本级验收行为不变。

### Requirement 7: 达到轮数上限后诚实上报

**User Story:** 作为用户，我要求在版面目标没达成时被如实告知——本特性正是因为之前有智能体谎报成功而存在。

#### Acceptance Criteria

1. WHEN 版面目标经 Bounded_Re_Edit_Loop 后仍未达成 THE 系统 SHALL 向用户如实报告未达成，并列出具体版面缺陷。
2. THE 系统 SHALL NOT 在版面目标未达成时报告成功。
3. WHEN 使用 LibreOffice_Backend 得出裁定 THE 系统 SHALL 在上报中附带保真度告警，说明结果在用户的 Microsoft Word 中不保证一致。

### Requirement 8: 能力 / 成本与外泄同意的主开关（非每轮强制）

**User Story:** 作为用户，我要能决定「是否允许启用这个能力」——因为它要花钱、且可能把我的论文页面图发到外部视觉 API；但这个开关只表示「允许用」，不表示「每轮都跑」。

#### Acceptance Criteria

1. THE Visual_Acceptance_Gate SHALL 由一个配置主开关控制**是否可用**，默认关闭；关闭时管线行为与现状逐字节保持不变。
2. THE 配置主开关 SHALL 表达「允许使用该能力 + 同意其成本与（外部多模态 API 时的）数据外泄」，而**不**表示「每轮任务都强制渲染 + 视觉调用」——具体某轮是否触发由 Requirement 11 的场景选择性触发决定。
3. WHERE 主开关关闭 THE 系统 SHALL 不执行任何渲染与视觉调用，也不派生 Visual_Judge_SubAgent。
4. THE 系统 SHALL 提供可配置的最大重编辑轮数与渲染 DPI，未配置时使用文档化默认值。
5. WHEN 一次评估执行 THE 系统 SHALL 使单轮评估的成本为一次渲染加一次视觉调用，并受最大轮数上限约束。
6. THE 主开关（是否允许 + 成本 / 外泄同意）SHALL 由用户 / 配置持有，不得由主智能体在运行时自行开启。

### Requirement 9: 不触碰正确性核心（非目标边界）

**User Story:** 作为对学术正确性负责的产品，我要求这道视觉闸只看版面、只驱动重编辑，绝不改动引用 / 内容 / 忠实性逻辑。

#### Acceptance Criteria

1. THE Visual_Acceptance_Gate SHALL 只读取 / 观察已产出的产物并驱动有界重编辑，不修改 Correctness_Core 的逻辑或数据。
2. THE Visual_Acceptance_Gate SHALL 不替代 Preservation_Check 与文本级验收闸，而是在其之后追加的一道建议性把关。
3. THE Visual_Acceptance_Gate SHALL 仅在 docx 编辑通过 Preservation_Check 之后触发。

### Requirement 10: 数据外泄与隐私考量

**User Story:** 作为用户，我要知道我的论文页面在被送去视觉判断时是否离开了本机。

#### Acceptance Criteria

1. THE 系统 SHALL 在本机执行渲染与栅格化。
2. WHERE Multimodal_LLM 为外部 API THE 系统 SHALL 将「Page_Images 可能包含用户论文内容并发生外部数据外泄」标注为一项须知考量。

### Requirement 11: 场景选择性触发（确定性版面触发 + 可选智能体主动调用，不盲跑）

**User Story:** 作为用户，我不希望每轮任务都盲目做视觉校验（大多数任务没有版面后果）；但我也不希望「要不要校验」完全交给那个会谎报成功的主智能体自己决定——它最该被检查时恰恰会觉得自己成功而跳过。

#### Acceptance Criteria

1. WHERE 主开关开启 且 本轮完成的工作包含 Layout_Affecting_Operation THE 系统 SHALL 自动触发一次 Visual_Acceptance_Gate 评估（确定性触发，不依赖主智能体的自我判断）。
2. WHERE 主开关开启 且 本轮**不含** Layout_Affecting_Operation 且 主智能体未主动请求 THE 系统 SHALL NOT 触发渲染与视觉调用（不盲跑，省成本）。
3. THE 系统 SHALL 额外把视觉校验暴露为一个主智能体**可主动调用**的工具，使其在自认为需要时请求一次评估。
4. THE 确定性触发（第 1 条）SHALL 独立于主智能体的意愿——主智能体**不能**跳过对其自身 Layout_Affecting_Operation 产物的视觉校验。
5. WHEN Layout_Affecting_Operation 的判定执行 THE 系统 SHALL 依据本轮实际发生的版面相关操作（如图跨栏、分栏、宽表适配、字体 / 字号、跨格式转 docx）做**确定性**判定，不调用 LLM 决定是否触发。
