# Paper Agent 评测

评测分为两层，二者用途不同：

1. **Mock smoke**：零网络、确定性运行，只验证编排、持久化、护栏和导出没有被改坏。
2. **Real accuracy**：使用真实 LLM、真实检索源以及真实或脱敏研究材料，衡量论文准确率。

Mock 数据不能证明论文质量。它只能作为快速回归门禁；引用忠实性、事实准确性和写作质量
必须由真实案例评测。

当前 Mock LLM 会给回显正文加 `[mock]` 前缀，而引用扫描会把方括号标记视为候选引用。
因此内置 smoke 案例不把“虚构引用数”作为通过条件；该指标只应在真实 provider 案例中
作为准确率硬门禁。

## 快速运行

```bash
python scripts/run_eval.py
```

结果写入 `eval/results/<run_id>/`：

- `summary.json`：整次评测摘要、配置与各案例结果；
- `<case_id>.json`：单案例指标和诊断；
- `<case_id>/trace.jsonl`：该案例的步骤追踪；
- `<case_id>/workspace/`：最终工作区与导出产物。

默认 trace 使用 `redacted`，避免真实论文内容被完整写入追踪文件。

## 运行真实准确率评测

先复制真实案例模板：

```bash
copy eval\cases\real\generation_with_real_data.json.example eval\cases\real\generation_001.json
```

修改其中的主题与 `artifact_dir`，然后配置真实 provider：

```bash
set PAPER_LLM=openai
set PAPER_LLM_MODEL=<writer-model>
set PAPER_RETRIEVAL=api
set PAPER_REVIEWER_LLM=<independent-reviewer-provider>
set PAPER_REVIEWER_LLM_MODEL=<independent-reviewer-model>
python scripts/run_eval.py --cases eval/cases/real --iteration-limit 3
```

真实评测默认开启声明级引用忠实性审计。需要下载开放全文用于 grounding 时：

```bash
set PAPER_GROUNDING_FULLTEXT=1
```

全文下载会增加耗时，并可能涉及第三方网络与论文版权；只应对允许处理的材料启用。

## 应该提供什么真实数据

优先使用以下数据：

- 自己已公开或已脱敏的论文初稿；
- 结构化 `ResearchArtifact`，包含真实方法、贡献、实验配置和结果 CSV；
- DOI、arXiv 或 OpenAlex 可核验的真实文献；
- 已知应该拒绝的困难案例，例如缺实验数据却要求生成实验结果；
- 人工确认过的任务要求和硬约束。

不要提交到 GitHub：

- 未公开论文全文；
- 含个人信息、密钥或受限数据集的数据；
- 无权再分发的出版社 PDF。

真实 fixtures 建议保存在仓库外，通过案例里的相对/绝对路径引用。公开评测集只放许可明确、
可再分发或自行构造的脱敏材料。

## 案例格式

案例使用 JSON。开放式生成不比较固定答案，而是声明必须满足的约束：

```json
{
  "id": "real_case_001",
  "requires_real_providers": true,
  "input": {
    "topic_background": "真实主题",
    "artifact_dir": "path/to/artifact",
    "output_format": "markdown"
  },
  "assertions": {
    "export_created": true,
    "max_fabricated_citations": 0,
    "max_fabricated_metrics": 0,
    "max_unsupported_citations": 0
  }
}
```

当前支持：

- `run_completed`
- `export_created`
- `expected_format`
- `submittable`
- `terminated_reason_in`
- `required_sections`
- `required_terms`
- `forbid_terms`
- `max_<diagnostic>`
- `min_<diagnostic>`
- `max_total_tokens` / `max_duration_s` / `max_llm_calls`
- `requires_independent_reviewer`
- `ingest_rejected`（用于预期摄入失败的负向案例）

常用 diagnostic 包括 `high_quality_issues`、`fabricated_citations`、
`fabricated_metrics`、`unsupported_citations`、`cannot_verify_citations`、
`verified_references`、`empty_sections` 和 `section_count`。

## 建议运行策略

- 开发过程中：运行 Mock smoke；
- Prompt、模型或检索逻辑变化：运行完整真实集；
- 发布前：真实集 + 5–10 个案例的人工匿名 A/B 复核；
- 不要仅使用系统自己的 ReviewAgent 分数作为准确率。
