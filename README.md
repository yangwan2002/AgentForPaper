# Paper Agent — 学术论文写作多智能体系统

面向学术论文写作的多智能体协作系统。借鉴通用智能体在**上下文管理、记忆管理、工具管理**上的解耦思想，采用清晰的模块边界与可插拔接口，支持「骨架优先、模块递进」的增量开发。

规格文档见 `.kiro/specs/academic-paper-writing-agent/`（requirements / design / tasks）。

## 当前进度

- ✅ **Phase 0 地基**：工作区数据模型、JSON 持久化、原子更新与失败回滚
- ✅ **Phase 1 最小骨架**：Agent 接口、Mock provider、五个智能体、Orchestrator 反馈循环、Markdown 导出，端到端可跑通两种输入模式
- ✅ **Phase 2 模块加深**：写作分章节 + 局部修改、评审四维评分与反馈循环、上下文管理模块、ToolRegistry + 引用真实性核验、arXiv/Semantic Scholar 真实检索、LaTeX + BibTeX 导出
- ✅ **Phase 3 完善**：输入模式差异化、图表与数据处理、docx 导出、OpenAI LLM provider、MCP 检索 provider

全部 16 个任务完成，测试套件 19 个用例全绿。

## 架构分层（单向依赖）

```
编排层 Orchestrator
  └─ 智能体层 Plan / Search / Query / Writing / Review
       └─ 能力层 ContextManager / PaperWorkspace / Tools / CitationVerifier
            └─ Provider 抽象层 LLMProvider / RetrievalProvider / WorkspaceStore / DocumentExporter
```

所有外部能力与智能体均通过抽象接口暴露，可插拔、可替换、可 mock。共享状态收敛到单一真相源 `PaperWorkspace`，智能体不直接写工作区，而是返回更新意图由仓储统一原子落盘。

## 目录结构

```
src/paper_agent/
├── workspace/      # 数据模型、持久化、仓储（单一真相源）
├── providers/      # llm/ 与 retrieval/ 的可插拔实现（含 mock）
├── agents/         # 五个智能体 + Agent 接口
├── tools/          # 引用真实性核验
├── export/         # 输出格式导出器（markdown，后续 latex/docx）
├── orchestrator.py # 工作流与反馈循环
├── app.py          # 依赖装配
└── config.py       # 配置
```

## 运行测试

```bash
pip install -e ".[dev]"
pytest
```

## 命令行（Round 11：一条命令，自动决策）

给一个**初稿文件**或一个**主题**即可，系统据文件类型自动选择处理方式：

```bash
python scripts/run_real.py my_draft.tex      # LaTeX → 保结构原地润色
python scripts/run_real.py my_draft.docx     # Word  → 保结构原地润色
python scripts/run_real.py my_draft.md        # md/txt/pdf → 完整重渲染管线
python scripts/run_real.py "我的论文主题"      # 无初稿 → 从零生成
```

- **默认交互**：系统拿不准时（缺章节/缺引用/缺数据/研究描述）会问你几个问题；加
  `--yes` 全程非交互、取最保守默认。
- **输出格式默认 = 输入格式**（`.tex` 进 `.tex` 出）；`PAPER_OUTPUT` 可覆盖。
- 参数从「10 个开关」瘦身为：位置参数（文件或主题）+ `--yes` / `--resume` /
  `--rebuild`；`--artifact` / `--profile` / `--styles` 降级为可选进阶项。
- 文件类型路由/输出格式等**纯决策逻辑**收敛到 `src/paper_agent/entry.py`（可单测）。

> 迁移说明：旧的 `--latex-inplace` / `--docx-inplace` / `--interactive` /
> `--no-interview` 开关已移除——原地润色改由**文件后缀自动路由**，交互**默认开启**，
> `--no-interview` 合并为 `--yes`。

## 快速使用（全 mock，零外部依赖）

```python
from paper_agent.app import build_orchestrator
from paper_agent.config import Config
from paper_agent.orchestrator import PaperRequest
from paper_agent.providers.llm.mock import MockLLMProvider
from paper_agent.providers.retrieval.mock import MockRetrievalProvider

orch = build_orchestrator(
    llm=MockLLMProvider(),
    retrieval=MockRetrievalProvider(),
    config=Config(),
)
result = orch.run(PaperRequest(topic_background="多智能体协作论文写作"))
print(result.terminated_reason, result.export.files)
```

## 会议样式文件（用户提供）

学术会议模板没有统一格式：每个会议各有自己的 `.cls`/`.sty`/`.bst`（如 NeurIPS 的 `neurips_2024.sty`、ICML 的 `icml2024.sty`、ACL 的 `acl.sty`）。系统不内置这些受版权约束的文件，需你自行获取：

- 从会议官网 CFP 页面或 Overleaf 模板库下载对应年份的样式包；
- IEEE 的 `IEEEtran.cls` 随 TeX Live 发行，通常无需单独提供。

把下载到的 `.sty`/`.cls` 放进一个样式目录，然后通过以下任一方式指定该目录：

```bash
python scripts/run_real.py "主题" --styles path/to/styles
# 或环境变量
PAPER_STYLES=path/to/styles
```

```python
Config(styles_dir="path/to/styles", venue_id="neurips")
```

会议通过 `PAPER_VENUE=neurips|icml|acl|ieee|default`（或 `Config(venue_id=...)`）选择。导出时系统会在样式目录中按文件名发现样式文件、复制到导出目录并在 `.tex` 中正确引用。

若某会议所需的样式文件缺失，系统会优雅降级为默认的 `article` 模板，并在产物中标注「已降级」说明，导出流程不中断。


## 投递质量增强（Submission-Quality Enhancements）

在既有多智能体管线之上，新增了一组面向「可投递」目标的能力，全部遵守既有契约
（单一写入路径、依赖倒置、优雅降级、Mock provider 下逐字节不变）：

### 1. 从零生成的结构化研究描述（反 hallucination 源头）

从零生成模式若未提供 `--artifact`，且以 `--interactive` 运行，系统会经**澄清问答器**
询问三个必填项——**研究领域、所解决的问题、所用方法**（以及可选的贡献与新颖性），
据此构造一个最小 `ResearchArtifact`，把生成从「全凭 LLM 编造」拉回到「基于用户真实
研究方向」。

```bash
python scripts/run_real.py "论文主题" --interactive   # 触发澄清问答
python scripts/run_real.py "论文主题"                  # 非交互：取最保守默认
```

程序化用法见 `paper_agent.ingestion.interactive_intake.run_intake`（接受一个
`Elicitor`）。

### 2. 语言润色 / 一致性校对智能体

反馈循环收敛后、导出前运行一次独立语言 pass（`LanguagePolishAgent`）：逐章节修正
语法、统一术语、消除中英混排与套话，**同时确定性守卫保真**——引用标注 `[id]`、
数字、章节结构一旦被破坏即丢弃润色、保留原文。Mock provider 下自动 no-op。
开关：`Config.language_polish_enabled`（默认开）。

### 3. 原创性 / 相似度自检

导出前对每章做与已核验文献的 n-gram 重合度自检（`tools/originality_check.py`，
纯确定性、无 LLM），高重合章节记为可投递性 **caution**（提示人工复核，不阻断导出）。
开关：`Config.originality_check_enabled`、`originality_ngram`、`originality_overlap_threshold`。

### 4. 可投递性硬判定（Submittability）

`PaperResult` 新增 `submittable: bool` 与 `submittability_notes: list[str]`。任一硬约束
不满足即标记为**不可直接投递**：
- 从零生成但无真实研究内容（LLM 推断版）；
- 反馈循环未以 `quality_met` 终止（质量未达标 / 评审不可信 / 停滞 / 预算超额）；
- 目标格式未通过校验或已降级（无法保证正确编译/版式）；
- 存在空章节。

判定说明并入 `ExportResult.notes` 并经事件上报，CLI 结尾打印「可投递：是/否」及原因。

### 交叉模型评审（已支持）

「writer 与 reviewer 用不同模型」以打破自评 reward-hack 的能力此前已存在，经
`Config.reviewer_llm_*` 配置注入独立的 reviewer provider/model/端点即可启用。


## LaTeX 原地润色模式（in-place source polish）

常规「草稿修订」是**内容驱动、重渲染**——抽取正文后经自己的模板重排，不保留你原始
的 LaTeX 源结构。若你想要**保住自己的 `.tex` 源（preamble/宏/公式/环境/图表/引用
逐字不动），只在原地润色自然语言**，用这个独立模式：

```bash
python scripts/run_real.py --draft path/to/draft.tex --latex-inplace
# 产出 output/draft.polished.tex（需配置真实 PAPER_LLM，Mock 下为 no-op）
```

工作原理（`src/paper_agent/latex_inplace.py`）：

1. **结构保护分段**：把源切成不重叠、全覆盖的 `PROSE` / `PROTECTED` 段。保护范围含
   preamble、`\end{document}` 之后、注释、行内/行间数学、公式/表格/图/代码等环境、
   以及 `\cite`/`\ref`/`\label`/`\includegraphics`/章节命令等带参命令。不确定的一律
   归入保护。往返无损：`"".join(段) == 原文`。
2. **只润散文**：仅把 `PROSE` 片段送 LLM 润色（语法/术语/中英混排/衔接）。
3. **确定性守卫**：润色前后，反斜杠命令多重集合、`{}[]$` 计数、数字多重集合、
   `[id]` 引用集合必须完全一致，且长度浮动在允许区间内——任一不满足即**丢弃润色、
   保留原文**。即便分段偶有疏漏，守卫也能防止结构被破坏。

因此该模式产出的 `.tex` 与你的原稿在结构上**逐字一致**，只有自然语言被润色，可直接
用你原来的编译流程编译。

## DOCX 原地润色模式（in-place source polish）

对标 LaTeX 原地模式，解决"给 `.docx` 初稿 → 保留 Word 全部排版 → 只润文字"的诉求。
此前 docx 导出是**从零重建**（丢字体/样式/编号/页眉页脚/表格/图/脚注/修订），本模式
把用户的 `.docx` **当作真相**：

```bash
python scripts/run_real.py --draft path/to/draft.docx --docx-inplace
# 产出 output/draft.polished.docx（需真实 PAPER_LLM；Mock 下复制原文 no-op）
```

工作原理（`src/paper_agent/docx_inplace.py`）：

1. **只重写正文散文段落的 `run.text`**——用 python-docx 打开原文、只改文字、绝不
   re-emit document，故 OOXML 结构（sectPr / styles / numbering / headerReference /
   `w:tbl` / `w:drawing` / `w:hyperlink` / 脚注 / 批注 / 修订）**自然逐字保留**。
2. **保守跳过**：表格内段落、页眉页脚、结构型样式段落（Heading/Title/TOC/Caption/
   Bibliography/脚注）、以及含超链接/域/脚注引用/内嵌图形对象的段落——一律原样保留。
3. **只润格式同质段落**：段内所有 run 格式一致时才把润色文本并入首个 run、清空其余；
   异质段落（含局部加粗等）跳过，避免丢失段内格式。
4. **确定性保真守卫**：复用 `polish_guards`（引用/数字恒等、长度受限），任一破坏丢弃该段。
5. **文档级结构 diff 闸 + 回滚**：写盘前比对 pre/post 结构签名（段落/表格/图形/超链接/
   脚注计数、标题文本、sectPr），任一不等即**整档回滚为原文副本**，绝不产出结构被破坏
   的文件。产物原子落盘、且**永不覆盖输入文件**。


## 澄清式交互（human-in-the-loop）

当系统对"下一步该怎么做"拿不准时（典型：草稿修订时初稿只有方法+实验、缺引言/结论），
不再擅自猜测，而是**抛出问题让用户选择**，再据答案继续——借鉴 Claude/Cursor 的做法。

### 统一的问答抽象 `Elicitor`（`src/paper_agent/elicitation.py`）

与只出的 `EventSink` 对称的只进通道 `Elicitor.ask(question)`，依赖注入、三种实现：

- `CLIElicitor`：终端交互（`--interactive` 时使用）。
- `ScriptedElicitor`：测试注入固定答案，确定可测。
- `AutoElicitor`：非交互/CI/Mock 一律取问题默认值——不阻塞、行为确定、既有批处理
  与测试逐字节不变（默认）。

之前的"从零生成研究描述"问答已重构为此抽象下的一个流程，全局只有一套问答风格。

### 确定性澄清（`src/paper_agent/clarification.py`）

草稿修订时，编排器在规划后运行澄清阶段：用 `section_types` **确定性**检测初稿缺失的
常规章节（引言/相关工作/结论），据用户选择产出并记录一个 `RevisionScope`
（仅语言润色 / +补全章节 / +补充文献）到 `ws.profile['revision_scope']`：

- 决策**持久化** → 续跑不重复问、整轮可复现。
- 用户选择补章节 → 经单一写入路径把对应 `OutlineNode` 追加进大纲，写作智能体后续生成。
- 非交互默认「仅语言润色」→ 不改动大纲，向后兼容。

```bash
python scripts/run_real.py --draft draft.tex --interactive   # 就修订范围/补章节征询
```

### 与 LaTeX 原地润色的关系

`latex_inplace` 是**执行引擎**（保结构只润散文），澄清问答是**决策层**（决定范围）——
两者是不同层、互补而非替代。LaTeX 原地模式只改语言；若检测到初稿缺常规章节，会**诚实
提示**你改用（重渲染）草稿修订模式 `--interactive` 去补全，而不是默默略过。

### 动态澄清问题（路径 B：LLM 据场景提问，受约束）

除固定问题外，可让 LLM 针对具体论文提出我们没预置的澄清点（借鉴 Claude/Cursor
的"拿不准就问"）。为避免问题疲劳与不可控，做了三重约束（`clarification.ClarificationProposer`）：

- **数量上限** `Config.max_clarifying_questions`（默认 3，截断）；
- **仅结构化解析成功才采用**——Mock/失败返回空列表，不问凑数问题；
- **只在澄清阶段调用一次**（非连续对话），且**仅交互式 Elicitor 才触发**——非交互
  下连提出器都不调用（零额外 LLM 开销）。

开关 `Config.llm_clarifying_questions_enabled`（默认关闭；CLI `--interactive` 时自动开启）。
答案记入 `ws.profile['clarification_answers']`，并作为「用户澄清偏好」注入写作 prompt，
从而真正影响产出、且可复现/续跑不重复。

### 写作期按需提问 `ask_user`（mid-loop，仅 WritingAgent）

很多信息缺口只有写到具体章节才暴露——缺失的实验数字、未定义的术语、某条声明缺
具体引用来源。这类"只有作者才知道、且答案会实质改变本节内容"的缺口，非交互管线只能
靠质量闸/忠实性审计**拒绝或删除**，无法**向作者要到**那条信息。为此给 WritingAgent
的工具循环加了一个 `ask_user` 工具（`tools/ask_user_tool.py`），让它在写作中按需向作者
提问：

- **仅交互式 Elicitor 才注册**该工具——非交互下写作智能体根本不暴露它（零影响）。
- **配额上限**（默认 3）防写作期狂问；**按问题哈希缓存**——同一问题不重复问，且续跑时
  从 `ws.profile['clarification_answers']` 回放已有答案。
- **工具不直接写工作区**：新问答由 WritingAgent 经 `AgentResult.mutations` 单一写入路径落盘。
- 用户不可用/超额/留空时，工具明确提示模型**自行合理处理、切勿编造**数字或引用。

**只有 WritingAgent 拥有此工具**；ReviewAgent / 对抗审 / 忠实性审计作为"裁判"一律不给
——避免评判独立性被污染。SearchAgent 的按需提问（空结果/歧义）价值中等，列为后续。

> 为什么不做"随时按需提问"（Cursor 式的 ask_user 工具）？因为本系统是批处理管线而非
> 连续对话——生成到一半阻塞等输入会与非交互/续跑/CI 冲突。受约束的"规划期提问"在灵活
> 与可控之间取得了更合适的平衡。

### 共享守卫去重

语言润色与 LaTeX 原地润色共用 `src/paper_agent/tools/polish_guards.py` 的保真守卫
（引用/数字/长度，LaTeX 另加命令与括号计数），不再各写一份。


## 论文质量增强（Round 8：体裁化润色/评审 + 主动术语抽取）

针对审查报告指出的「润色/评审无体裁差异、术语一致性靠外部注入」三项质量缺口：

### 1. 体裁化语言润色

`LanguagePolishAgent` 现按章节体裁（`section_types.infer_and_get_spec`）向润色 prompt
注入该体裁的语言/结构惯例（摘要更凝练、方法更精确等）。因 `POLISH_SYSTEM` 硬约束
「不新增事实/数字/引用、不改结构」，且下游确定性守卫兜底，越界改动会被丢弃——只提升
语言，不越权改内容。

### 2. 体裁化评审强制项

`ReviewAgent` 汇总大纲各章节体裁的 `review_rubric` 注入评审 prompt，使评审据体裁做
强制检查（摘要五要素、方法可复现性、引言 motivation-gap-contribution、实验基线/消融/
显著性等）——此前 `review_rubric` 定义了却无消费者。

### 3. 主动术语抽取

新增 `TerminologyAgent`（`Config.terminology_extraction_enabled`，默认开）：语言润色**之前**
读全文、经 `StructuredParser` 抽取核心术语的规范写法写入 `ws.glossary`（`setdefault`，
不覆盖用户已提供），随后润色据此统一全篇用词。Mock provider 下自身 no-op。

以上均为加法式接入、遵守单一写入路径；Mock/停用时逐字节不变。


## 引用忠实性：grounding 扩展到被引论文正文（Round 9）

审查报告指出引用忠实性的主要假阴来源：grounding 只到 **abstract 层**——真实论文大量
细节声明在被引论文**正文**，abstract 里没有 → 被迫 `cannot_verify`。本轮把 grounding
的取材上限从 abstract 提升到正文：

- **数据模型**：`ReferenceEntry` 新增可选 `full_text`（默认空，向后兼容；旧 JSON 缺键
  回落空串）。
- **grounding 消费**：`assemble_grounding` 在 `full_text` 非空时，按同一套段落切片
  （`slice_section`，与 abstract 共用逻辑，不重造）从正文取 method/results/motivation/
  conclusion 段并入 grounding。`full_text` 为空时**逐字节回落**到 abstract 层（行为不变）。
- **富化来源**：新增 `tools/reference_enrichment.py`——依赖倒置的 `FullTextFetcher`
  （`fetch(url)->str|None`）+ 纯函数 `collect_full_texts`（可 stub 测试、不写工作区）；
  默认实现 `PdfUrlFetcher` 下载 PDF 后复用既有 PDF 解析取正文，**全程 best-effort**
  （任何失败返回 None）。
- **接入**：编排器在检索后、反馈循环前运行富化阶段（经单一写入路径落盘 `full_text`），
  使循环内的忠实性审计做**正文级 grounding**。由 `Config.grounding_fulltext_enabled`
  控制（默认关闭，因涉及网络）；未注入抓取器时整段跳过、行为不变。


## 第二轮整改（Round 10：正确性/安全 + 文件类型自动路由）

### 文件类型自动路由（消除「按直觉跑毁格式」）

`--draft foo.docx` / `foo.tex` **默认自动走保结构原地润色**，不再落到会丢排版的重渲染
管线。`.md/.txt/.pdf` 仍走完整管线。想强制完整重渲染加 `--rebuild`（会丢原排版）。

### DOCX 原地润色加固

- **tracked-changes 安全**：含 `w:ins`/`w:del`（修订痕迹）的段落一律跳过，避免「接受修订」
  后文本 = 润色版 + 旧删除版拼接的语义错乱。
- **结构 part 兜底签名**：结构 diff 闸除 body 计数外，新增 styles/numbering/settings/
  headers/footers/footnotes/endnotes/comments 等 part 的 **SHA-256** 比对（用 baseline/final
  双临时文件消除 python-docx 重序列化噪声）；任一变动即整档回滚。
- **表格段知情**：统计并在结果 `notes` 报告「跳过 N 个表格内段落」（表格整体保留、未润色）。

### 引用核验：缓存 + 有界重试

`CitationVerifier` 加进程内缓存（按 source_id / title）+ 对 `RetrievalError` 的有界退避
重试——消除「同一引用反复打网络」与「单次网络抖动即误报引用不存在」。

### 其它护栏

- `stable_block` 术语表按 key 排序 → 稳定前缀、提升前缀缓存命中率（省 token）。
- `run_real.py` 生产默认设 `total_token_budget=200万`（`PAPER_TOKEN_BUDGET` 可覆盖）。
- `enable_pdflatex_check` 改为**探测到 pdflatex 才开**（装了才校验，不误伤没装 TeX 的机器）。

### DOCX 结构校验单一真相源 + 挂进 Format_Gate（批次 4C）

此前 docx 的「结构判定」逻辑散落在两处、且互相矛盾：`docx_inplace` 自带一套结构签名/
part-SHA，而 `Format_Gate` 对 docx 只做「pandoc 能否解析」的**语法级伪校验**——查不出
「产物跟原文结构像不像」。批次 4C 把它收敛为单一真相源并接进闸门：

- **新增 `src/paper_agent/export/docx_structural.py`（单一真相源）**：`structural_signature` /
  `structural_fields` / `structural_part_shas` / `style_is_protected` / `qn_localname` /
  `docx_structural_diff_check` 全部集中于此。`docx_inplace` 改为从本模块导入，删除其中
  重复的 `_structural_signature` / `_el_to_str` / `_structural_part_shas` 定义（消除「屎山」、
  避免两份逻辑漂移）。
- **`docx_structural_diff_check(pre, post)`：面向任意两份 docx 的语义级结构 diff**——比对
  段落/表格/内嵌图形/超链接/脚注引用计数、标题文本集合、分节数，**对 python-docx 重序列化
  的字节噪声鲁棒**（不比 part 原始字节，避免假差异）。任一读取失败保守判为「结构不一致」。
- **挂进 `Format_Gate.docx_structural_diff_check(pre, post)`**：当存在原文（如原地润色的输入）
  时，闸门可对「产物 vs 原文」做真正的「结构像不像」校验，补上 `check()` 只能验「产物本身
  可否解析」的盲区。全程不调用 LLM、只读两份文件（符合 Format_Gate 契约 Req 9.5 / 9.9）；
  刻意**不**强塞进通用 `check()`（重建路径没有「原文」可比）。
