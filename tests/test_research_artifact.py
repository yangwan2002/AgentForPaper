"""Round 7 修复回归测试：ResearchArtifact 反 hallucination。"""

from __future__ import annotations

import json
import os

import pytest

from paper_agent.agents.adversarial_review_agent import AdversarialReviewAgent
from paper_agent.agents.base import AgentContext
from paper_agent.agents.writing_agent import WritingAgent
from paper_agent.config import Config
from paper_agent.context.manager import ContextManager
from paper_agent.ingestion import ArtifactLoadError, load_artifact
from paper_agent.orchestrator import Orchestrator, PaperRequest
from paper_agent.prompts import templates
from paper_agent.providers.llm.mock import MockLLMProvider
from paper_agent.providers.retrieval.mock import MockRetrievalProvider
from paper_agent.tools.quality_gate import QualityGate, build_allowed_values
from paper_agent.workspace.models import (
    InputMode,
    OutlineNode,
    PaperWorkspace,
    SectionDraft,
)
from paper_agent.workspace.repository import WorkspaceRepository
from paper_agent.workspace.research_artifact import (
    Contribution,
    Experiment,
    MethodSpec,
    ResearchArtifact,
)
from paper_agent.workspace.store import InMemoryStore


# --------------------------------------------------------------------------- #
# ResearchArtifact 数据类
# --------------------------------------------------------------------------- #


def test_research_artifact_roundtrip():
    """ResearchArtifact to_dict / from_dict 往返序列化。"""
    artifact = ResearchArtifact(
        research_question="大视角差空地匹配",
        method=MethodSpec(
            overview="OETR + cross-view consistency loss",
            key_components=["OETR", "Cross-view consistency loss"],
        ),
        contributions=[Contribution(summary="提升 8.3% mAP", evidence_refs=["main"])],
        experiments=[
            Experiment(
                experiment_id="main",
                dataset="University-1652",
                baselines=["OETR", "LPN"],
                results_data={
                    "columns": ["method", "mAP"],
                    "rows": [
                        {"method": "OETR", "mAP": 72.3},
                        {"method": "Ours", "mAP": 83.4},
                    ],
                    "stats": {
                        "mAP": {"mean": 76.075, "std": 4.34, "min": 72.3, "max": 83.4},
                    },
                },
            )
        ],
        novelty_claims=["首次引入 cross-view consistency"],
    )
    data = artifact.to_dict()
    restored = ResearchArtifact.from_dict(data)
    assert restored.research_question == artifact.research_question
    assert restored.method.overview == artifact.method.overview
    assert len(restored.contributions) == 1
    assert restored.contributions[0].summary == "提升 8.3% mAP"
    assert len(restored.experiments) == 1
    assert restored.experiments[0].experiment_id == "main"
    assert restored.novelty_claims == ["首次引入 cross-view consistency"]


def test_research_artifact_all_numeric_values():
    """all_numeric_values 从 results_data 抽取数值。"""
    artifact = ResearchArtifact(
        research_question="test",
        method=MethodSpec(overview="test"),
        experiments=[
            Experiment(
                experiment_id="main",
                hyperparameters={"ransac_px": 2.5},
                results_data={
                    "columns": ["mAP", "Recall"],
                    "rows": [
                        {"mAP": 72.3, "Recall": 86.1},
                        {"mAP": 83.4, "Recall": 93.2},
                    ],
                    "stats": {},
                },
            )
        ],
    )
    values = artifact.all_numeric_values()
    assert 72.3 in values
    assert 83.4 in values
    assert 86.1 in values
    assert 93.2 in values
    assert 2.5 in values


def test_allowed_values_include_percent_and_same_metric_delta():
    artifact = ResearchArtifact(
        research_question="test",
        method=MethodSpec(overview="test"),
        experiments=[
            Experiment(
                experiment_id="ablation",
                results_data={
                    "rows": [{"mIoU": 0.884}, {"mIoU": 0.818}],
                    "stats": {},
                },
            )
        ],
    )
    allowed = build_allowed_values(artifact)
    assert any(abs(value - 88.4) < 1e-9 for value in allowed)
    assert any(abs(value - 0.066) < 1e-9 for value in allowed)


def test_research_artifact_is_empty():
    """is_empty 判断 artifact 是否实质为空。"""
    empty = ResearchArtifact(research_question="", method=MethodSpec(overview=""))
    assert empty.is_empty()
    non_empty = ResearchArtifact(
        research_question="test", method=MethodSpec(overview="overview")
    )
    assert not non_empty.is_empty()


# --------------------------------------------------------------------------- #
# artifact_loader
# --------------------------------------------------------------------------- #


def _write_minimal_artifact(tmp_path):
    """写一个最小可跑的 artifact 目录。"""
    artifact_dir = tmp_path / "artifact"
    artifact_dir.mkdir()
    experiments_dir = artifact_dir / "experiments"
    experiments_dir.mkdir()

    yaml_content = """research_question: 大视角差空地匹配

method:
  overview: OETR + cross-view consistency loss
  key_components:
    - OETR
    - Cross-view consistency loss

contributions:
  - summary: 提升 8.3% mAP
    evidence_refs:
      - main

experiments:
  - experiment_id: main
    dataset: University-1652
    baselines: [OETR, LPN]
    results_csv: experiments/main.csv
"""
    (artifact_dir / "artifact.yaml").write_text(yaml_content, encoding="utf-8")

    csv_content = """method,mAP
OETR,72.3
LPN,73.5
Ours,83.4
"""
    (experiments_dir / "main.csv").write_text(csv_content, encoding="utf-8")

    return artifact_dir


def test_load_artifact_minimal(tmp_path):
    """最小 artifact 目录能正确加载。"""
    artifact_dir = _write_minimal_artifact(tmp_path)
    artifact = load_artifact(str(artifact_dir))
    assert artifact.research_question == "大视角差空地匹配"
    assert artifact.method.overview == "OETR + cross-view consistency loss"
    assert len(artifact.contributions) == 1
    assert artifact.contributions[0].summary == "提升 8.3% mAP"
    assert len(artifact.experiments) == 1
    assert artifact.experiments[0].experiment_id == "main"
    # CSV 解析结果。
    results = artifact.experiments[0].results_data
    assert "rows" in results
    assert len(results["rows"]) == 3
    assert results["stats"]["mAP"]["max"] == 83.4


def test_load_artifact_missing_required_field(tmp_path):
    """缺少必填字段时报 ArtifactLoadError。"""
    artifact_dir = tmp_path / "artifact"
    artifact_dir.mkdir()
    # 缺少 research_question 和 method.overview。
    yaml_content = """method:
  overview: ""

contributions: []
experiments: []
"""
    (artifact_dir / "artifact.yaml").write_text(yaml_content, encoding="utf-8")
    with pytest.raises(ArtifactLoadError, match="research_question"):
        load_artifact(str(artifact_dir))


def test_load_artifact_missing_csv_graceful(tmp_path):
    """CSV 文件不存在时不报错，results_data 含 _error 字段。"""
    artifact_dir = tmp_path / "artifact"
    artifact_dir.mkdir()
    yaml_content = """research_question: test
method:
  overview: overview
contributions:
  - summary: c1
experiments:
  - experiment_id: main
    results_csv: nonexistent.csv
"""
    (artifact_dir / "artifact.yaml").write_text(yaml_content, encoding="utf-8")
    artifact = load_artifact(str(artifact_dir))
    # CSV 不存在 → results_data 含 _error 字段。
    results_data = artifact.experiments[0].results_data
    assert "_error" in results_data
    assert "CSV" in results_data["_error"]


def test_load_artifact_notes_md(tmp_path):
    """notes.md 自动拼接到 artifact.notes。"""
    artifact_dir = _write_minimal_artifact(tmp_path)
    (artifact_dir / "notes.md").write_text("# 补充说明\n\n实验在 4 卡 3090 上跑。", encoding="utf-8")
    artifact = load_artifact(str(artifact_dir))
    assert "4 卡 3090" in artifact.notes


def test_load_artifact_json_fallback(tmp_path):
    """JSON 格式的 artifact 也能加载（YAML 1.2 是 JSON 超集）。"""
    artifact_dir = tmp_path / "artifact"
    artifact_dir.mkdir()
    json_content = {
        "research_question": "test",
        "method": {"overview": "overview"},
        "contributions": [{"summary": "c1"}],
        "experiments": [{"experiment_id": "main"}],
    }
    (artifact_dir / "artifact.json").write_text(
        json.dumps(json_content, ensure_ascii=False), encoding="utf-8"
    )
    artifact = load_artifact(str(artifact_dir))
    assert artifact.research_question == "test"


# --------------------------------------------------------------------------- #
# Workspace 序列化集成 artifact
# --------------------------------------------------------------------------- #


def test_workspace_roundtrip_with_artifact(tmp_path):
    """PaperWorkspace 序列化含 artifact 时能正确往返。"""
    ws = PaperWorkspace(
        workspace_id="w",
        input_mode=InputMode.GENERATION,
        topic_background="test",
    )
    ws.artifact = ResearchArtifact(
        research_question="rq",
        method=MethodSpec(overview="method"),
        contributions=[Contribution(summary="c1")],
    )
    data = ws.to_dict()
    restored = PaperWorkspace.from_dict(data)
    assert restored.artifact is not None
    assert restored.artifact.research_question == "rq"


def test_workspace_from_dict_without_artifact():
    """旧 JSON 无 artifact 字段时 from_dict 不报错（向后兼容）。"""
    data = {
        "workspace_id": "w",
        "input_mode": "generation",
        "topic_background": "test",
        "outline": [],
        "task_checklist": [],
        "glossary": {},
        "verified_references": [],
        "section_drafts": {},
        "section_summaries": {},
        "draft_sections": {},
        "figures": [],
        "review_records": [],
        "adversarial_records": [],
        "iteration": 0,
        "citation_audit": [],
        "quality_report": [],
        "retrieval_completed": False,
        "profile": {},
        "created_at": "2026-01-01T00:00:00+00:00",
        "updated_at": "2026-01-01T00:00:00+00:00",
    }
    ws = PaperWorkspace.from_dict(data)
    assert ws.artifact is None


# --------------------------------------------------------------------------- #
# QualityGate grounding 检查
# --------------------------------------------------------------------------- #


def _ws_with_artifact_and_draft(content: str) -> PaperWorkspace:
    """构造含 artifact 和章节草稿的工作区。"""
    ws = PaperWorkspace(
        workspace_id="w",
        input_mode=InputMode.GENERATION,
        topic_background="test",
    )
    ws.artifact = ResearchArtifact(
        research_question="test",
        method=MethodSpec(overview="method"),
        experiments=[
            Experiment(
                experiment_id="main",
                results_data={
                    "columns": ["mAP"],
                    "rows": [
                        {"mAP": 72.3},
                        {"mAP": 83.4},
                    ],
                    "stats": {
                        "mAP": {"mean": 77.85, "std": 5.55, "min": 72.3, "max": 83.4},
                    },
                },
            )
        ],
    )
    ws.outline = [OutlineNode(section_id="intro", title="引言", order=0)]
    ws.section_drafts = {
        "intro": SectionDraft(section_id="intro", title="引言", content=content)
    }
    return ws


def test_quality_gate_grounding_passes():
    """正文数字在 artifact 中能找到 → 不报 fabricated_metric。"""
    ws = _ws_with_artifact_and_draft(
        "我们的方法达到 83.4 mAP，基线为 72.3。" * 5
    )
    report = QualityGate().check(ws)
    fabricated = [i for i in report.issues if i["type"] == "fabricated_metric"]
    assert not fabricated


def test_quality_gate_ignores_structural_decimal_numbers():
    """章节、图表和公式编号不应被当作实验指标。"""
    ws = _ws_with_artifact_and_draft(
        "# 第1章 方法\n\n## 1.2 网络结构\n见第1.3节、图 3.4 与式 2.5。"
        + "真实结果为 83.4 mAP。" * 5
    )
    report = QualityGate().check(ws)
    fabricated = [i for i in report.issues if i["type"] == "fabricated_metric"]
    assert not fabricated


def test_numeric_extraction_does_not_split_decimal_tail():
    values = QualityGate._extract_numeric_values(
        "mIoU为0.884，ATE为0.407 [openalex:W3047057232]。"
    )
    assert values == [0.407, 0.884]


def test_quality_gate_grounding_flags_fabricated():
    """正文数字在 artifact 中找不到 → 报 fabricated_metric (high)。"""
    ws = _ws_with_artifact_and_draft(
        "我们的方法达到 99.9% mAP，完美解决所有问题。" * 5
    )
    report = QualityGate().check(ws)
    fabricated = [
        i for i in report.issues if i["type"] == "fabricated_metric"
    ]
    assert fabricated
    assert any("99.9" in i["message"] for i in fabricated)
    assert report.passed is False


def test_quality_gate_grounding_skips_without_artifact():
    """无 artifact 时跳过 grounding 检查（向后兼容）。"""
    ws = PaperWorkspace(
        workspace_id="w",
        input_mode=InputMode.GENERATION,
        topic_background="test",
    )
    ws.outline = [OutlineNode(section_id="intro", title="引言", order=0)]
    ws.section_drafts = {
        "intro": SectionDraft(
            section_id="intro",
            title="引言",
            content="我们的方法达到 99.9% mAP。" * 5,
        )
    }
    report = QualityGate().check(ws)
    assert not any(
        i["type"] == "fabricated_metric" for i in report.issues
    )


# --------------------------------------------------------------------------- #
# WritingAgent artifact 注入
# --------------------------------------------------------------------------- #


def test_writing_agent_injects_artifact_context():
    """WritingAgent._run_context 含 artifact 时注入摘要。"""
    ws = PaperWorkspace(
        workspace_id="w",
        input_mode=InputMode.GENERATION,
        topic_background="test",
    )
    ws.artifact = ResearchArtifact(
        research_question="大视角差空地匹配",
        method=MethodSpec(
            overview="OETR + cross-view consistency loss",
            key_components=["OETR", "Cross-view consistency loss"],
        ),
        contributions=[Contribution(summary="提升 8.3% mAP")],
        experiments=[
            Experiment(
                experiment_id="main",
                results_data={
                    "rows": [{"method": "ours", "mAP": 83.4}],
                    "stats": {},
                },
            )
        ],
    )
    llm = MockLLMProvider()
    agent = WritingAgent(llm, ContextManager(MockLLMProvider()))
    context = agent._run_context(ws)
    assert "用户真实研究内容" in context
    assert "大视角差空地匹配" in context
    assert "OETR + cross-view consistency loss" in context
    assert '"mAP": 83.4' in context


def test_writing_agent_no_artifact_no_block():
    """WritingAgent._run_context 无 artifact 时不注入 artifact block。"""
    ws = PaperWorkspace(
        workspace_id="w",
        input_mode=InputMode.GENERATION,
        topic_background="test",
    )
    llm = MockLLMProvider()
    agent = WritingAgent(llm, ContextManager(MockLLMProvider()))
    context = agent._run_context(ws)
    assert "用户真实研究内容" not in context


# --------------------------------------------------------------------------- #
# AdversarialReviewAgent artifact 注入
# --------------------------------------------------------------------------- #


def test_adversarial_review_prompt_includes_artifact():
    """对抗审 prompt 含 artifact_context 时注入真实研究内容。"""
    artifact_context = "【用户提供的真实研究内容】\n研究问题：test"
    messages = templates.adversarial_review_paper(
        paper_text="论文正文",
        min_weaknesses=3,
        artifact_context=artifact_context,
    )
    user_msg = messages[-1].content
    assert "用户提供的真实研究内容" in user_msg
    assert "fabricated_content" in user_msg  # category 列表含此类别


def test_adversarial_review_no_artifact_omits_context():
    """对抗审 prompt 无 artifact_context 时不注入 artifact block。"""
    messages = templates.adversarial_review_paper(
        paper_text="论文正文",
        min_weaknesses=3,
    )
    user_msg = messages[-1].content
    assert "用户提供的真实研究内容" not in user_msg


# --------------------------------------------------------------------------- #
# Orchestrator GENERATION 模式无 artifact 警告
# --------------------------------------------------------------------------- #


class _RecordingSink:
    """记录所有事件，便于断言。"""

    def __init__(self) -> None:
        self.events: list = []

    def emit(self, event) -> None:
        self.events.append(event)


def test_orchestrator_warns_generation_without_artifact(tmp_path):
    """GENERATION 模式无 artifact 时发出警告事件。"""
    sink = _RecordingSink()
    orch = Orchestrator(
        repo=WorkspaceRepository(InMemoryStore()),
        plan_agent=_FakePlan(),
        search_agent=_FakeNoop("search"),
        writing_agent=_FakeNoop("writer"),
        review_agent=_FakeNoop("reviewer"),
        config=Config(workspace_dir=str(tmp_path), iteration_limit=1),
        sink=sink,
    )
    result = orch.run(PaperRequest(topic_background="test"))
    # 检查是否有警告事件。
    warnings = [
        e for e in sink.events
        if "GENERATION 模式无 artifact" in str(e.message)
    ]
    assert warnings


def test_orchestrator_no_warning_when_artifact_present(tmp_path):
    """GENERATION 模式有 artifact 时不发出警告。"""
    sink = _RecordingSink()
    orch = Orchestrator(
        repo=WorkspaceRepository(InMemoryStore()),
        plan_agent=_FakePlan(),
        search_agent=_FakeNoop("search"),
        writing_agent=_FakeNoop("writer"),
        review_agent=_FakeNoop("reviewer"),
        config=Config(workspace_dir=str(tmp_path), iteration_limit=1),
        sink=sink,
    )
    artifact = ResearchArtifact(
        research_question="test",
        method=MethodSpec(overview="method"),
    )
    result = orch.run(PaperRequest(topic_background="test", artifact=artifact))
    warnings = [
        e for e in sink.events
        if "GENERATION 模式无 artifact" in str(e.message)
    ]
    assert not warnings


# --------------------------------------------------------------------------- #
# 辅助 fake agents
# ---------------------------------------------------------------------------


from paper_agent.agents.base import Agent, AgentResult  # noqa: E402


class _FakePlan(Agent):
    name = "plan"

    def run(self, ctx: AgentContext) -> AgentResult:
        def mut(w):
            w.outline = [OutlineNode(section_id="intro", title="引言", order=0)]

        return AgentResult(mutations=[mut])


class _FakeNoop(Agent):
    def __init__(self, name: str) -> None:
        self.name = name

    def run(self, ctx: AgentContext) -> AgentResult:
        return AgentResult()
