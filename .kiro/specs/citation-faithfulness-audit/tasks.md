# Implementation Plan: citation-faithfulness-audit（引用忠实性审计）

## Overview

本计划把设计文档拆解为一系列增量式、测试驱动的编码任务，落在既有 `src/paper_agent/` 代码库上。总体路径：先建数据模型与配置（可序列化、向后兼容、默认关闭），再实现三个纯函数子步骤（抽取 / grounding 组装 / 严重度映射）与注入式判定器，随后组装 `CitationFaithfulnessAgent`，最后按「加法式、默认关闭安全」的方式接入 `Orchestrator` 反馈闭环与 `app.build_orchestrator` 装配层。

严格保持既有契约：单一写入路径（`AgentResult.mutations` → `WorkspaceRepository.update`）、依赖倒置（`LLMProvider` / `StructuredParser` 经注入）、复用既有能力（`quality_gate.extract_text_citations`、`paper_section_tool.extract_section`、`StructuredParser`、`ReferenceEntry`），不改动 `CitationVerifier` / `CitationAuditAgent` / `QualityGate` 的职责，默认关闭时行为逐字节不变。

属性测试统一用 `hypothesis`（≥100 迭代），每条以 `# Feature: citation-faithfulness-audit, Property N: ...` 注释标注；stub/spy `StructuredParser` 与判定器以覆盖降级路径。

## Tasks

- [x] 1. 数据模型：裁决枚举、瞬态对、可序列化发现与工作区字段
  - [x] 1.1 新增 `src/paper_agent/workspace/faithfulness.py`
    - 定义 `FaithfulnessVerdict(str, Enum)`：`SUPPORTED / WEAK_SUPPORT / UNSUPPORTED / CANNOT_VERIFY`
    - 定义瞬态 `@dataclass ClaimCitationPair`：`section_id / claim_sentence / cited_reference_id`（不序列化）
    - 定义可序列化 `@dataclass CitationFaithfulnessFinding`：`section_id / cited_reference_id / claim_excerpt / verdict / severity / rationale / supporting_snippet / parse_status / unverified_reference`，实现 `to_dict()` 与 `from_dict()`（`verdict` 用枚举 `.value` 序列化、反序列化时容错回落 `cannot_verify`）
    - _Requirements: 4.1, 5.1, 5.3_

  - [x] 1.2 在 `src/paper_agent/workspace/models.py` 的 `PaperWorkspace` 增加 `citation_faithfulness: list[dict] = field(default_factory=list)`
    - `to_dict()` 增加 `"citation_faithfulness": list(self.citation_faithfulness)`（镜像 `citation_audit` / `quality_report` 写法）
    - `from_dict()` 增加 `citation_faithfulness=list(data.get("citation_faithfulness", []))`——缺字段默认空列表，旧 JSON 反序列化不失败
    - _Requirements: 5.3, 5.4, 9.5_

  - [x]* 1.3 编写属性测试：报告序列化 round-trip 与向后兼容默认（文件 `tests/test_faithfulness_props_serialize.py`）
    - **Property 13: 报告序列化 round-trip 与向后兼容默认**
    - **Validates: Requirements 5.3, 5.4, 9.5**

  - [x]* 1.4 编写单元测试：`from_dict` 对不含 `citation_faithfulness` 键的旧 JSON 回落空列表（文件 `tests/test_faithfulness_models_unit.py`）
    - _Requirements: 5.4_

- [x] 2. 配置项与范围校验
  - [x] 2.1 在 `src/paper_agent/config.py` 的 `Config` 增加字段并扩展 `validate()`
    - 新增 `citation_faithfulness_enabled: bool = False`（默认关闭）、`min_grounding_chars: int = 40`、`faithfulness_token_budget: int = 4000`
    - `validate()` 增加范围校验：`min_grounding_chars >= 0`、`faithfulness_token_budget >= 1`；非法时回退到文档化默认（40 / 4000）并记录该回退，不抛致命异常
    - _Requirements: 8.3, 8.4_

  - [x]* 2.2 编写单元测试：非法阈值（负数 / 非数值）触发回退到默认并记录（文件 `tests/test_faithfulness_config_unit.py`）
    - _Requirements: 8.4_

- [x] 3. PairExtractor：声明-引用对抽取（纯函数，复用既有扫描）
  - [x] 3.1 新增 `src/paper_agent/tools/faithfulness_extract.py`
    - `split_sentences(text) -> list[tuple[int,int,str]]`：确定性句子切分，边界 = CJK `。！？` + ASCII `.!?` + 换行，连续边界折叠，返回每句 `(start, end, sentence)`
    - `extract_pairs(section_id, content, verified_ids) -> tuple[list[ClaimCitationPair], list[ClaimCitationPair]]`：直接复用 `quality_gate._TEXT_CITATION` 对同一正则做 `finditer` 取标注字符位置，用 `split_sentences` 找包含该位置的完整句子作 `claim_sentence`；同句多个不同 id 各产一对且 `(sentence, ref_id)` 去重；`ref_id ∉ verified_ids` 进入 `unverified_pairs`；空正文/无标注返回两个空列表
    - _Requirements: 1.1, 1.2, 1.3, 1.4, 1.5, 1.6, 9.3_

  - [x]* 3.2 编写属性测试：引用扫描规则复用一致性（文件 `tests/test_faithfulness_props_extract.py`）
    - **Property 1: 引用扫描规则复用一致性**（抽取到的 id 集合逐一等于 `extract_text_citations(content)`）
    - **Validates: Requirements 1.1, 9.3**

  - [x]* 3.3 编写属性测试：声明句包含其标注且为完整句子（文件 `tests/test_faithfulness_props_extract.py`）
    - **Property 2: 声明句包含其标注且为完整句子**
    - **Validates: Requirements 1.2, 1.3**

  - [x]* 3.4 编写属性测试：同句多引用逐一成对且去重（文件 `tests/test_faithfulness_props_extract.py`）
    - **Property 3: 同句多引用逐一成对且去重**
    - **Validates: Requirements 1.4**

  - [x]* 3.5 编写单元测试：空正文、无 `[id]`、含非引用方括号（如 `[表格 第1页]`）不产对（文件 `tests/test_faithfulness_extract_unit.py`）
    - _Requirements: 1.6_

- [x] 4. GroundingAssembler：grounding 文本组装（纯函数，复用 extract_section）
  - [x] 4.1 新增 `src/paper_agent/tools/faithfulness_grounding.py`
    - `assemble_grounding(ref, *, token_budget, section_hints=("method","results","motivation","conclusion")) -> str`：仅从 `ref.title + ref.abstract + ref.abstract_sections` 取材；复用 `paper_section_tool.extract_section(ref, name)` 抽取命中段落，不新增第二套抽取；确定性拼接顺序（title → 命中段落 → abstract 兜底）去重后 `strip`，再防御式截断至 `token_budget` 上限字符数；绝不调用 `LLMProvider`、绝不引入被引文献之外文本
    - _Requirements: 2.1, 2.2, 2.3, 2.4, 2.6, 7.4, 9.3_

  - [x]* 4.2 编写属性测试：grounding-only 不变量（文件 `tests/test_faithfulness_props_grounding.py`）
    - **Property 5: grounding-only 不变量**（输入仅由 claim + grounding + 元信息构成；grounding 每部分来自该 ref 的 title/abstract/abstract_sections；改被引文献之外字段不改变输入；组装不调 LLM）
    - **Validates: Requirements 2.1, 2.4, 3.1**

  - [x]* 4.3 编写属性测试：喂入判定器的文本受 token_budget 上限约束（文件 `tests/test_faithfulness_props_grounding.py`）
    - **Property 7: 喂入判定器的文本受 token_budget 上限约束**
    - **Validates: Requirements 2.6, 7.4**

  - [x]* 4.4 编写单元测试：构造带 `abstract_sections` 的 ref，断言 grounding 含 `extract_section` 命中段落（文件 `tests/test_faithfulness_grounding_unit.py`）
    - _Requirements: 2.2, 2.3_

- [x] 5. 判定 prompt 模板
  - [x] 5.1 在 `src/paper_agent/prompts/templates.py` 增加判定模板
    - 新增 `FAITHFULNESS_JUDGE_SYSTEM`（仅依据 grounding 判定、不得用自身知识/记忆、grounding 不足必选 `cannot_verify`、仅输出 JSON）
    - 新增 `judge_citation_faithfulness(*, claim, grounding, reference_meta) -> list[Message]`：稳定段 = system + 判定规范，易变段 = claim + grounding + reference_meta；要求输出 `{"verdict","rationale","supporting_snippet"}`
    - _Requirements: 3.1_

  - [x]* 5.2 编写单元测试：模板消息仅含 claim/grounding/reference_meta（无其它章节正文/记忆提示），且声明所需 JSON 键（文件 `tests/test_faithfulness_templates_unit.py`）
    - _Requirements: 3.1_

- [x] 6. FaithfulnessJudge 与严重度映射（注入 StructuredParser）
  - [x] 6.1 新增 `src/paper_agent/agents/citation_faithfulness_agent.py`，实现 `FaithfulnessJudge` 与 `severity_for`
    - `FaithfulnessJudge.__init__(self, parser: StructuredParser)`（依赖注入，不内部实例化）
    - `judge(*, claim, grounding, reference_meta) -> tuple[FaithfulnessVerdict, str, str, ParseStatus]`：经 `parser.request_json(templates.judge_citation_faithfulness(...), required_keys=("verdict",))`；`PARSED` → 取 `data['verdict']`，属枚举则采用、否则 `cannot_verify`；`MOCK_FALLBACK` / `FAILED` → `cannot_verify` 并记 reason；永不在非 PARSED 时返回 `supported`
    - `severity_for(verdict) -> str`：全函数覆盖四值（`unsupported→high`、`weak_support→medium`、`cannot_verify→low`、`supported→none`）
    - _Requirements: 3.2, 3.3, 3.4, 3.5, 3.6, 4.1, 4.2, 4.3, 4.4, 4.5_

  - [x]* 6.2 编写属性测试：PARSED 采用合法枚举、非法值降级（文件 `tests/test_faithfulness_props_judge.py`）
    - **Property 8: PARSED 采用合法枚举，非法值降级**
    - **Validates: Requirements 3.3**

  - [x]* 6.3 编写属性测试：非 PARSED 绝不 supported（核心安全属性）（文件 `tests/test_faithfulness_props_judge.py`）
    - **Property 9: 非 PARSED 或 grounding 不足绝不 supported**（stub parser 返回 FAILED/MOCK_FALLBACK/任意 verdict/抛异常，验证唯有 PARSED 才可 supported）
    - **Validates: Requirements 3.4, 3.5, 3.6, 7.1**

  - [x]* 6.4 编写属性测试：verdict 全域属于枚举且严重度映射为全函数（文件 `tests/test_faithfulness_props_judge.py`）
    - **Property 10: verdict 全域属于枚举且严重度映射为全函数**
    - **Validates: Requirements 4.1, 4.2, 4.3, 4.4, 4.5**

  - [x]* 6.5 编写单元测试：spy `StructuredParser`，断言 `judge` 以 `required_keys=("verdict",)` 调用 `request_json`（文件 `tests/test_faithfulness_judge_unit.py`）
    - _Requirements: 3.2, 9.2_

- [x] 7. CitationFaithfulnessAgent：编排三步 → 单条 mutation
  - [x] 7.1 在 `src/paper_agent/agents/citation_faithfulness_agent.py` 实现 `CitationFaithfulnessAgent(Agent)`
    - `__init__(self, judge, *, min_grounding_chars, token_budget, is_mock=False, sink=None)`
    - `run(ctx)`：遍历 `ws.section_drafts` 调 `extract_pairs`；未验证 id 直接成 `unverified_reference=True` 的 `cannot_verify` finding（不调判定器）；每对经 `assemble_grounding`，`strip` 后为空或 `< min_grounding_chars` → `cannot_verify`（不调判定器），否则调 `judge`；逐对 `try/except` → `cannot_verify` 并 `continue`；每条 finding 用 `severity_for` 定严重度、透传 `rationale`/`supporting_snippet`；聚合所有 `CitationFaithfulnessFinding.to_dict()` 为 `list[dict]`，返回**单条** mutate 闭包**替换**写 `ws.citation_faithfulness`；观测日志经 `self._sink`（文本片段限长、不含密钥）
    - _Requirements: 1.5, 2.5, 4.6, 5.1, 5.2, 5.5, 7.1, 7.3, 7.5, 7.6, 9.1_

  - [x]* 7.2 编写属性测试：未验证引用标记且不触发判定器（文件 `tests/test_faithfulness_props_safety.py`）
    - **Property 4: 未验证引用标记且不触发判定器**（spy 判定器验证零调用）
    - **Validates: Requirements 1.5**

  - [x]* 7.3 编写属性测试：grounding 不足即安全落 cannot_verify（文件 `tests/test_faithfulness_props_degrade.py`）
    - **Property 6: grounding 不足即安全落 cannot_verify**（不调判定器）
    - **Validates: Requirements 2.5**

  - [x]* 7.4 编写属性测试：报告与对一一对应且字段完备（文件 `tests/test_faithfulness_props_report.py`）
    - **Property 11: 报告与对一一对应且字段完备**（发现条数 = 抽取到的对总数含未验证对；每条含 section_id/cited_reference_id/claim_excerpt/verdict/severity/parse_status）
    - **Validates: Requirements 5.1**

  - [x]* 7.5 编写属性测试：单一写入路径（文件 `tests/test_faithfulness_props_report.py`）
    - **Property 12: 单一写入路径**（`run` 恰返回一条 mutation；执行 `run` 本身不改动传入工作区的 `citation_faithfulness`）
    - **Validates: Requirements 5.2, 9.1**

  - [x]* 7.6 编写属性测试：再次运行替换而非累加（文件 `tests/test_faithfulness_props_serialize.py`）
    - **Property 14: 再次运行替换而非累加**（依次应用两次 mutation 后只反映最后一次结果）
    - **Validates: Requirements 5.5, 9.5**

  - [x]* 7.7 编写属性测试：单对异常隔离（文件 `tests/test_faithfulness_props_degrade.py`）
    - **Property 17: 单对异常隔离**（某对判定抛异常 → 该对 `cannot_verify` 并记因，其余照常，报告总条数不变，审计不中止）
    - **Validates: Requirements 7.6**

  - [x]* 7.8 编写属性测试：不可信文本纯字符串处理（文件 `tests/test_faithfulness_props_safety.py`）
    - **Property 18: 不可信文本纯字符串处理**（grounding/claim 含 `__import__`、`eval(...)`、模板注入 → 全程只做字符串处理，不 eval/exec、无副作用）
    - **Validates: Requirements 7.3**

  - [x]* 7.9 编写属性测试：阈值可配置且被采用（文件 `tests/test_faithfulness_props_threshold.py`）
    - **Property 19: 阈值可配置且被采用**（`min_grounding_chars` 边界处 `cannot_verify` 翻转；截断长度随 `token_budget` 变化）
    - **Validates: Requirements 8.3**

  - [x]* 7.10 编写单元测试：stub parser 返回带 `rationale`/`supporting_snippet`，断言透传到 `unsupported`/`weak_support` 发现（文件 `tests/test_faithfulness_agent_unit.py`）
    - _Requirements: 4.6_

- [x] 8. Checkpoint - 确保核心组件测试通过
  - Ensure all tests pass, ask the user if questions arise.

- [x] 9. Orchestrator 接入反馈闭环（加法式、默认关闭安全）
  - [x] 9.1 在 `src/paper_agent/orchestrator.py` 接入忠实性阶段
    - `Orchestrator.__init__` 新增可选参数 `faithfulness_agent: Agent | None = None`（缺省 None → 现状行为）
    - 新增 `_faithfulness_phase(ws)`，在 `_feedback_loop` 内每轮 `_review`（及对抗审）之后、`_build_edits` 之前经 `_run_agent` 调用（仅当 `self._faithfulness is not None`）
    - `_build_edits` 增加合并通道：读 `ws.citation_faithfulness`，把 `verdict == "unsupported"` 的发现按 `section_id` 并入 `gate_fixes`（复用既有 `gate_fixes.setdefault(sid, []).append(msg)`）
    - 新增 `_faithfulness_ok(ws)`：未装配 → 恒 True；否则无 `unsupported` 发现才 True；与 `llm_ok / gate_ok / adversarial_ok` **AND** 合并到 `quality_met` 判据
    - 不改动 `QualityGate` / `ReviewRecord` / `AdversarialReviewRecord` 契约
    - _Requirements: 6.1, 6.2, 6.3, 6.4, 6.5, 8.1_

  - [x]* 9.2 编写属性测试：unsupported 驱动定位式 high 修订项（文件 `tests/test_faithfulness_props_feedback.py`）
    - **Property 15: unsupported 驱动定位式 high 修订项**（`_build_edits` 为对应 section_id 增修订项；当且仅当无 unsupported/未装配时 `_faithfulness_ok` 为真）
    - **Validates: Requirements 6.1, 6.3**

  - [x]* 9.3 编写属性测试：停用时逐字节不变（文件 `tests/test_faithfulness_props_feedback.py`）
    - **Property 16: 停用时逐字节不变**（未装配 / 停用时 `_build_edits` 输出与达标判定与「本特性不存在」逐字节相同）
    - **Validates: Requirements 6.4, 6.5, 8.1**

  - [x]* 9.4 编写集成测试：构造含 `unsupported` 发现的工作区跑 `_feedback_loop`，断言 `WritingAgent` 收到 `gate_fixes[section_id]` 且该轮不判「忠实性达标」（文件 `tests/test_faithfulness_feedback_integration.py`）
    - _Requirements: 6.1, 6.3_

- [x] 10. 装配层接入（`app.build_orchestrator`）
  - [x] 10.1 在 `src/paper_agent/app.py` 的 `build_orchestrator` 装配 agent
    - 仅当 `config.citation_faithfulness_enabled` 为真时构造 `CitationFaithfulnessAgent(FaithfulnessJudge(reviewer_parser), min_grounding_chars=..., token_budget=config.faithfulness_token_budget, is_mock=reviewer_is_mock, sink=sink)`——复用 reviewer LLM 栈 + reviewer parser（判定属评审型任务，经既有 Observable 包装自动记录用量）
    - 经新增可选参数注入 `Orchestrator`（默认 None → 不接入、行为不变）
    - _Requirements: 7.2, 8.1, 8.2, 9.2_

  - [x]* 10.2 编写集成 smoke 测试：`citation_faithfulness_enabled=True` + Mock LLM 跑一轮反馈循环，断言 `ws.citation_faithfulness` 被写入、mock 路径全 `cannot_verify`、管线正常导出（文件 `tests/test_faithfulness_smoke.py`）
    - _Requirements: 8.2_

  - [x]* 10.3 编写集成测试：注入带 `UsageTracker` 的 Observable LLM 栈跑一次审计，断言记录了 LLM 用量、事件文本受长度上限且不含密钥（文件 `tests/test_faithfulness_observability.py`）
    - _Requirements: 7.2, 7.5_

- [x] 11. Final Checkpoint - 确保全部测试通过
  - Ensure all tests pass, ask the user if questions arise.

## Notes

- 标记 `*` 的子任务为可选测试任务，可为快速 MVP 跳过；核心实现任务从不标记为可选。
- 每个任务引用具体需求编号以保证可追溯；属性测试各自标注设计文档中的 Property 编号与所验证的需求条款。
- 属性测试统一用 `hypothesis`（≥100 迭代），stub/spy `StructuredParser` 与判定器以覆盖 `PARSED`/`MOCK_FALLBACK`/`FAILED`/抛异常各降级路径。
- 契约保持：所有工作区写入经 `AgentResult.mutations` → `WorkspaceRepository`；不修改 `CitationVerifier` / `CitationAuditAgent` / `QualityGate` 职责；默认关闭时逐字节向后兼容。
- 同一测试文件的多条属性子任务被安排在不同 wave，避免并行写冲突。

## Task Dependency Graph

```json
{
  "waves": [
    { "id": 0, "tasks": ["1.1", "2.1", "4.1", "5.1"] },
    { "id": 1, "tasks": ["1.2", "3.1", "6.1", "2.2", "4.2", "4.4", "5.2"] },
    { "id": 2, "tasks": ["7.1", "1.3", "1.4", "3.2", "3.5", "6.2", "6.5", "4.3"] },
    { "id": 3, "tasks": ["9.1", "3.3", "6.3", "7.2", "7.3", "7.4", "7.6", "7.9", "7.10"] },
    { "id": 4, "tasks": ["10.1", "3.4", "6.4", "7.5", "7.7", "7.8", "9.2", "9.4"] },
    { "id": 5, "tasks": ["9.3", "10.2", "10.3"] }
  ]
}
```
