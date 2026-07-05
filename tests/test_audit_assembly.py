"""装配层接线测试（inplace-polish-audit · Task 4）。

验证 build_agent_app 据 config 构造 DraftAuditor 并注入 InplacePolishWorkflow；
mock provider 下降级（判定器为 None、检索不可用），不崩溃、不阻断润色。
"""

from __future__ import annotations

from paper_agent.agent_platform.app import build_agent_app
from paper_agent.agent_platform.audit import DraftAuditor
from paper_agent.agent_platform.routing import Intent
from paper_agent.config import Config


def _mock_config(tmp_path, *, audit_enabled=True):
    return Config(
        llm_provider="mock",
        retrieval_provider="mock",
        workspace_dir=str(tmp_path),
        inplace_audit_enabled=audit_enabled,
        routing_enabled=True,
    )


def test_build_agent_app_constructs_auditor_when_enabled(tmp_path):
    app = build_agent_app(_mock_config(tmp_path, audit_enabled=True))
    assert isinstance(app.draft_auditor, DraftAuditor)
    # mock provider → 检索不可用、判定器为 None（降级）。
    assert app.draft_auditor._retrieval_available is False
    assert app.draft_auditor._faithfulness_agent is None


def test_auditor_disabled_when_flag_off(tmp_path):
    app = build_agent_app(_mock_config(tmp_path, audit_enabled=False))
    assert app.draft_auditor is None


def test_routing_injects_auditor_into_inplace_workflow(tmp_path):
    app = build_agent_app(_mock_config(tmp_path, audit_enabled=True))
    # 构造一个会话的路由，确认 INPLACE_POLISH 工作流拿到了 auditor。
    from paper_agent.agent_platform.models import WritingTask

    session = app._intake.start(WritingTask("hi"), require_instruction=False)
    _agent, _ask, ctx = app._build_agent(session)
    _router, workflows = app._build_routing(ctx)
    inplace_wf = workflows[Intent.INPLACE_POLISH]
    assert inplace_wf._auditor is app.draft_auditor
