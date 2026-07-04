# Research Artifact 示例（最小可跑）

此目录是 ResearchArtifact 的最小示例——展示用户最少需要提供哪些内容，让
GENERATION 模式不再依赖 LLM 凭空编造。

## 文件清单

- `artifact.yaml`：主清单。含 research_question / method / contributions /
  experiments 四项必填字段。
- `experiments/main.csv`：实验真实结果。`artifact.yaml` 通过 `results_csv` 引用。
- `notes.md`：可选的自由格式补充说明。loader 会自动拼到 `artifact.notes`。

## 使用方式

```python
from paper_agent.ingestion import load_artifact
from paper_agent.orchestrator import PaperRequest

artifact = load_artifact("examples/minimal_artifact")
request = PaperRequest(
    topic_background="空地图像匹配",
    artifact=artifact,
)
```

或 CLI（待 Step B 接入 CLI 参数）：

```bash
python scripts/run_real.py "空地图像匹配" --artifact ./examples/minimal_artifact
```

## 字段说明

| 字段 | 必填 | 用途 |
|---|---|---|
| `research_question` | ✓ | 一句话研究问题，注入 Intro 与 Abstract |
| `method.overview` | ✓ | 方法概述，注入 Method 章节 |
| `contributions[]` | ✓ | 3-5 条贡献，注入 Intro/Conclusion 复述 |
| `experiments[]` | ✓ | 至少 1 条实验；含 dataset/baselines/metrics/hyperparams/results_csv |
| `code_repository` | — | 写 reproducibility 段时引用 |
| `novelty_claims[]` | — | 对抗审会专门验证这些 claim 是否真新颖 |
| `must_cite_refs[]` | — | 用户指定必须引用的 reference id（如已知关键工作） |
