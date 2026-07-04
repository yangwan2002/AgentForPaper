"""Phase 1 端到端冒烟测试：全 mock provider 跑通两种输入模式。"""

from __future__ import annotations

import pytest

from paper_agent.app import build_orchestrator
from paper_agent.config import Config
from paper_agent.orchestrator import InputValidationError, PaperRequest
from paper_agent.providers.llm.mock import MockLLMProvider
from paper_agent.providers.retrieval.mock import MockRetrievalProvider
from paper_agent.workspace.models import OutputFormat, ScoringDimension
from paper_agent.workspace.store import InMemoryStore


def _config(tmp_path) -> Config:
    return Config(
        quality_threshold=8.0,
        iteration_limit=5,
        default_output_format=OutputFormat.MARKDOWN,
        workspace_dir=str(tmp_path),
    )


def _build(tmp_path, store=None):
    return build_orchestrator(
        llm=MockLLMProvider(),
        retrieval=MockRetrievalProvider(),
        config=_config(tmp_path),
        store=store or InMemoryStore(),
    )


def test_generation_mode_end_to_end(tmp_path):
    orch = _build(tmp_path)
    result = orch.run(
        PaperRequest(topic_background="多智能体协作论文写作", )
    )
    # 纯 Mock provider 的评审为 MOCK_FALLBACK（parse_status != PARSED），评审不可信，
    # 故不会误判 quality_met，而是在迭代上限内以可诊断原因终止（Req 2.2/2.3/2.6/2.8）。
    assert result.terminated_reason == "iteration_limit_unparsed_review"
    # 优雅降级：即便评审不可信，导出仍应执行并产出文件（Req 10.2）。
    assert result.export is not None
    assert len(result.export.files) >= 1


def test_draft_revision_mode_end_to_end(tmp_path):
    orch = _build(tmp_path)
    draft = "# 引言\n背景...\n# 方法\n做法...\n# 结论\n总结..."
    result = orch.run(PaperRequest(draft=draft))
    # 同上：Mock 评审不可信，于迭代上限内终止并仍完成导出（优雅降级）。
    assert result.terminated_reason == "iteration_limit_unparsed_review"
    assert result.workspace_id
    assert result.export is not None


def test_missing_input_raises(tmp_path):
    orch = _build(tmp_path)
    with pytest.raises(InputValidationError):
        orch.run(PaperRequest())


def test_iteration_limit_termination_marks_unmet(tmp_path):
    """评分不可信/不达标时，应在迭代上限终止并标注未达标维度（Req 2.6/2.7/2.8）。"""
    cfg = _config(tmp_path)
    cfg.quality_threshold = 100.0  # 不可能达标
    orch = build_orchestrator(
        llm=MockLLMProvider(),
        retrieval=MockRetrievalProvider(),
        config=cfg,
        store=InMemoryStore(),
    )
    result = orch.run(PaperRequest(topic_background="主题"))
    # Mock 评审为 MOCK_FALLBACK（不可信），到达上限时以可诊断原因终止（Req 2.6）。
    assert result.terminated_reason == "iteration_limit_unparsed_review"
    assert set(result.unmet_dimensions) == set(ScoringDimension)


def test_export_file_written_to_disk(tmp_path):
    """导出环节应实际产出文件（Markdown）。"""
    orch = build_orchestrator(
        llm=MockLLMProvider(),
        retrieval=MockRetrievalProvider(),
        config=_config(tmp_path),
        store=InMemoryStore(),
    )
    result = orch.run(PaperRequest(topic_background="主题"))
    assert result.export is not None
    assert len(result.export.files) == 1
    import os

    assert os.path.exists(result.export.files[0])
