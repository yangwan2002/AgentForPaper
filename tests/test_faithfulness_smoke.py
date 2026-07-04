"""集成 smoke 测试：citation-faithfulness-audit 装配层接入（任务 10.2 / Req 8.2）。

验证「开启开关 + 全 Mock LLM 栈」时：

1. 一次完整的最小管线运行不崩溃、正常导出（优雅降级不受影响）；
2. ``ws.citation_faithfulness`` 字段被写入（运行后存在为 list）；
3. Mock 路径下所有裁决均为 ``cannot_verify``——Mock LLM 只产 MOCK_FALLBACK，
   判定器绝不给出 ``supported`` / ``weak_support``（核心安全属性，Req 3.4/3.5/7.1）。

复用既有 ``tests/test_orchestrator_smoke.py`` 的全 Mock 端到端装配模式
（``build_orchestrator`` + ``MockLLMProvider`` + ``MockRetrievalProvider`` +
``InMemoryStore``），仅追加 ``citation_faithfulness_enabled=True``。

环境说明：输出格式取 MARKDOWN——Markdown 从不经格式闸，故 pandoc 缺失
（本环境未安装）不影响导出；这与既有 smoke 测试的处理一致。
"""

from __future__ import annotations

from paper_agent.app import build_orchestrator
from paper_agent.config import Config
from paper_agent.orchestrator import Orchestrator, PaperRequest
from paper_agent.providers.llm.mock import MockLLMProvider
from paper_agent.providers.retrieval.mock import MockRetrievalProvider
from paper_agent.workspace.models import (
    InputMode,
    OutlineNode,
    OutputFormat,
    PaperWorkspace,
    ReferenceEntry,
    SectionDraft,
)
from paper_agent.workspace.store import InMemoryStore


def _config(tmp_path, *, enabled: bool = True) -> Config:
    """最小可用配置（Mock 模式），按需开启引用忠实性审计。"""
    return Config(
        quality_threshold=8.0,
        iteration_limit=3,
        default_output_format=OutputFormat.MARKDOWN,
        workspace_dir=str(tmp_path),
        citation_faithfulness_enabled=enabled,
    )


def _build(tmp_path, store, *, enabled: bool = True) -> Orchestrator:
    return build_orchestrator(
        llm=MockLLMProvider(),
        retrieval=MockRetrievalProvider(),
        config=_config(tmp_path, enabled=enabled),
        store=store,
    )


def test_pipeline_runs_with_faithfulness_enabled_and_exports(tmp_path):
    """开启审计 + 全 Mock 栈：管线跑通、正常导出、字段写入、Mock 路径全 cannot_verify。"""
    store = InMemoryStore()
    orch = _build(tmp_path, store, enabled=True)

    result = orch.run(PaperRequest(topic_background="多智能体协作论文写作"))

    # (3) 管线正常导出（Req 8.2）：即便 Mock 评审不可信，导出仍执行并产出文件。
    assert result.export is not None
    assert len(result.export.files) >= 1

    ws = store.load(result.workspace_id)
    assert ws is not None

    # (1) citation_faithfulness 字段被写入——运行后存在为 list（开启审计已装配）。
    assert isinstance(ws.citation_faithfulness, list)

    # (2) Mock 路径：报告中若有任何发现，其裁决必为 cannot_verify，绝不 supported。
    for finding in ws.citation_faithfulness:
        assert finding["verdict"] == "cannot_verify", (
            f"Mock 路径出现非 cannot_verify 裁决：{finding}"
        )


def test_faithfulness_report_populated_and_all_cannot_verify_on_mock_path(tmp_path):
    """确定性场景：草稿含指向已验证文献的 [id] 引用时，审计阶段写入非空报告，
    且 Mock 路径下所有裁决均为 cannot_verify。

    直接驱动经生产装配（``build_orchestrator``）接线好的忠实性审计阶段
    （``Orchestrator._faithfulness_phase``），在一个「含引用 + 已验证文献 +
    grounding 充足」的种子工作区上运行——保证抽取到声明-引用对、grounding 充足
    触达判定器，判定器经 Mock 栈返回 MOCK_FALLBACK → cannot_verify（Req 8.2）。
    """
    store = InMemoryStore()
    orch = _build(tmp_path, store, enabled=True)

    # 已验证文献：abstract 明显超过默认 min_grounding_chars=40，保证 grounding 充足，
    # 使判定器真正被调用（而非因 grounding 不足前置短路）。
    ref = ReferenceEntry(
        id="ref1",
        title="Retrieval-grounded citation verification",
        authors=["A. Author", "B. Author"],
        year=2023,
        source_id="arxiv:2301.00001",
        source="arxiv",
        verified=True,
        abstract=(
            "This paper studies claim-level, retrieval-grounded citation "
            "faithfulness verification for multi-agent scientific writing systems."
        ),
    )

    ws = PaperWorkspace(
        workspace_id="smoke_seed",
        input_mode=InputMode.GENERATION,
        output_format=OutputFormat.MARKDOWN,
        topic_background="多智能体协作论文写作",
    )
    ws.outline = [OutlineNode(section_id="intro", title="引言", order=0)]
    ws.verified_references = [ref]
    ws.section_drafts = {
        "intro": SectionDraft(
            section_id="intro",
            title="引言",
            content="本文研究检索-grounded 的引用忠实性校验方法 [ref1]。",
            cited_reference_ids=["ref1"],
        )
    }

    # 驱动真实接线好的忠实性审计阶段（单一写入路径经 WorkspaceRepository 落盘）。
    orch._faithfulness_phase(ws)

    findings = ws.citation_faithfulness
    # 报告非空：至少产出一条发现（指向 ref1 的声明-引用对）。
    assert isinstance(findings, list)
    assert len(findings) >= 1, "审计应为含引用的草稿写入非空报告"
    assert any(f["cited_reference_id"] == "ref1" for f in findings), (
        f"报告应包含指向 ref1 的发现：{findings}"
    )

    # Mock 路径：全部裁决为 cannot_verify（判定器经 MOCK_FALLBACK 安全降级，
    # 绝不产出 supported / weak_support）。
    for finding in findings:
        assert finding["verdict"] == "cannot_verify", (
            f"Mock 路径出现非 cannot_verify 裁决：{finding}"
        )
