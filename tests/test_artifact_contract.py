from paper_agent.agents.base import AgentContext
from paper_agent.agents.plan_agent import PlanAgent
from paper_agent.agents.writing_agent import WritingAgent
from paper_agent.providers.llm.mock import MockLLMProvider
from paper_agent.tools.artifact_commit_gate import (
    ArtifactCommitGate,
    build_claim_manifest,
)
from paper_agent.tools.quality_gate import QualityGate
from paper_agent.workspace.models import (
    InputMode,
    PaperWorkspace,
    SectionDraft,
)
from paper_agent.workspace.research_artifact import (
    Contribution,
    Experiment,
    MethodSpec,
    ResearchArtifact,
)


def _artifact() -> ResearchArtifact:
    return ResearchArtifact(
        research_question="如何提高跨视角图像匹配精度？",
        method=MethodSpec(
            overview="提出证据约束的跨视角匹配网络。",
            key_components=["尺度感知模块", "几何一致性模块"],
        ),
        contributions=[
            Contribution("提出尺度感知匹配方法", evidence_refs=["main"])
        ],
        experiments=[
            Experiment(
                experiment_id="main",
                dataset="MegaDepth",
                baselines=["LoFTR"],
                metrics=["AUC"],
                seed=7,
                results_data={"rows": [{"method": "ours", "AUC": 0.884}]},
            )
        ],
    )


def test_artifact_hash_ignores_local_directory() -> None:
    left = _artifact()
    right = _artifact()
    left.artifact_dir = "D:/private/a"
    right.artifact_dir = "D:/private/b"
    assert left.contract().artifact_hash == right.contract().artifact_hash


def test_contract_materializes_numeric_and_entity_whitelists() -> None:
    contract = _artifact().contract()
    assert 0.884 in contract.allowed_numeric_values
    assert 88.4 in contract.allowed_numeric_values
    assert "loftr" in contract.entity_evidence
    assert "baseline:main:loftr" in contract.entity_evidence["loftr"]


def test_planner_builds_evidence_bound_artifact_outline_without_llm() -> None:
    ws = PaperWorkspace(
        workspace_id="contract-plan",
        input_mode=InputMode.GENERATION,
        topic_background="cross-view matching",
        artifact=_artifact(),
    )
    outline, flags = PlanAgent(MockLLMProvider())._build_generation_outline(ws)
    assert flags == {}
    assert {node.section_id for node in outline} >= {
        "introduction",
        "method",
        "experiments",
        "conclusion",
    }
    assert all(node.allowed_evidence_ids for node in outline if node.section_id != "related_work")
    assert any(
        evidence_id == "experiment:main"
        for node in outline
        for evidence_id in node.required_evidence_ids
    )


def test_commit_gate_rejects_unknown_dataset_seed_and_metric() -> None:
    artifact = _artifact()
    ws = PaperWorkspace(
        workspace_id="contract-gate",
        input_mode=InputMode.GENERATION,
        artifact=artifact,
    )
    node = PlanAgent(MockLLMProvider())._artifact_outline(ws)[3]
    ws.outline = [node]
    content = "我们在 FakeSet 数据集实验，随机种子为 999，AUC 达到 0.999。"
    candidate = SectionDraft(
        section_id=node.section_id,
        title=node.title,
        content=content,
        artifact_hash=artifact.contract().artifact_hash,
        evidence_ids=list(node.allowed_evidence_ids),
        claim_manifest=build_claim_manifest(content, node.allowed_evidence_ids),
    )
    result = ArtifactCommitGate().check(ws, node, candidate)
    assert not result.passed
    assert {"unknown_dataset", "unknown_seed", "fabricated_metric"} <= {
        issue["type"] for issue in result.high_violations
    }


def test_commit_gate_rejects_semantically_wrong_claim_binding() -> None:
    artifact = _artifact()
    ws = PaperWorkspace(
        workspace_id="contract-semantics",
        input_mode=InputMode.GENERATION,
        artifact=artifact,
    )
    node = PlanAgent(MockLLMProvider())._artifact_outline(ws)[2]
    ws.outline = [node]
    claim = "LoFTR 在 MegaDepth 数据集的 AUC 为 0.884。"
    candidate = SectionDraft(
        section_id=node.section_id,
        title=node.title,
        content="### main\n" + claim,
        artifact_hash=artifact.contract().artifact_hash,
        evidence_ids=list(node.allowed_evidence_ids),
        claim_manifest=[
            {
                "claim": claim,
                "evidence_ids": ["dataset:main"],
                "kind": "artifact_fact",
            }
        ],
    )
    result = ArtifactCommitGate().check(ws, node, candidate)
    assert "claim_evidence_unsupported" in {
        issue["type"] for issue in result.high_violations
    }


def test_commit_gate_rejects_unknown_named_method() -> None:
    artifact = _artifact()
    ws = PaperWorkspace(
        workspace_id="contract-method",
        input_mode=InputMode.GENERATION,
        artifact=artifact,
    )
    node = PlanAgent(MockLLMProvider())._artifact_outline(ws)[1]
    ws.outline = [node]
    content = (
        "提出证据约束的跨视角匹配网络。\n"
        "尺度感知模块\n几何一致性模块\n采用 FakeNet 模型完成匹配。"
    )
    candidate = SectionDraft(
        section_id=node.section_id,
        title=node.title,
        content=content,
        artifact_hash=artifact.contract().artifact_hash,
        evidence_ids=list(node.allowed_evidence_ids),
        claim_manifest=build_claim_manifest(content, node.allowed_evidence_ids),
    )
    result = ArtifactCommitGate().check(ws, node, candidate)
    assert "unknown_method" in {
        issue["type"] for issue in result.high_violations
    }


def test_quality_gate_rejects_stale_artifact_hash() -> None:
    artifact = _artifact()
    ws = PaperWorkspace(
        workspace_id="contract-stale",
        input_mode=InputMode.GENERATION,
        artifact=artifact,
    )
    node = PlanAgent(MockLLMProvider())._artifact_outline(ws)[2]
    ws.outline = [node]
    ws.section_drafts[node.section_id] = SectionDraft(
        section_id=node.section_id,
        title=node.title,
        content="本文采用尺度感知模块和几何一致性模块完成匹配。",
        artifact_hash="outdated",
        evidence_ids=list(node.allowed_evidence_ids),
        claim_manifest=[],
    )
    assert "stale_artifact_hash" in {
        issue["type"] for issue in QualityGate(min_section_chars=1).check(ws).high_issues
    }


def test_revision_initial_pass_preserves_source_facts_byte_for_byte() -> None:
    source = "# 方法\nSAGENet 使用 Sim(3)。\n# 实验\nATE 为 0.407，N=3090。"
    ws = PaperWorkspace(
        workspace_id="revision-preserve",
        input_mode=InputMode.DRAFT_REVISION,
        original_draft=source,
        artifact=_artifact(),
    )
    for mutation in PlanAgent(MockLLMProvider()).run(
        AgentContext(workspace=ws)
    ).mutations:
        mutation(ws)
    for mutation in WritingAgent(MockLLMProvider()).run(
        AgentContext(workspace=ws)
    ).mutations:
        mutation(ws)
    assert ws.section_drafts["sec_0"].content == "SAGENet 使用 Sim(3)。"
    assert ws.section_drafts["sec_1"].content == "ATE 为 0.407，N=3090。"
