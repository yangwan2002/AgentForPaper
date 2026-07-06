"""装配层接线测试（sandboxed-run-python · Task 5）。

启用 → 注册 run_python 且可跑;未启用 → 工具集不含 run_python(向后兼容)。
"""

from __future__ import annotations

from paper_agent.agent_platform.app import build_agent_app
from paper_agent.agent_platform.models import WritingTask
from paper_agent.config import Config


def _config(tmp_path, *, enabled, backend="subprocess"):
    return Config(
        llm_provider="mock",
        retrieval_provider="mock",
        workspace_dir=str(tmp_path),
        run_python_enabled=enabled,
        sandbox_backend=backend,
    )


def _registry_tool_names(app):
    session = app._intake.start(WritingTask("hi"), require_instruction=False)
    _agent, _ask, ctx = app._build_agent(session)
    registry, _ = app._build_registry(ctx)
    return {t.name for t in registry.list_tools()}


def test_enabled_registers_run_python(tmp_path):
    app = build_agent_app(_config(tmp_path, enabled=True, backend="subprocess"))
    assert app.run_python_enabled is True
    assert app.sandbox_runner is not None
    names = _registry_tool_names(app)
    assert "run_python" in names


def test_disabled_omits_run_python(tmp_path):
    app = build_agent_app(_config(tmp_path, enabled=False))
    assert app.run_python_enabled is False
    names = _registry_tool_names(app)
    assert "run_python" not in names


def test_docker_unavailable_rejected(tmp_path, monkeypatch):
    from paper_agent.agent_platform.sandbox import DockerSandbox

    monkeypatch.setattr(DockerSandbox, "available", lambda self: False)
    app = build_agent_app(_config(tmp_path, enabled=True, backend="docker"))
    # 指定 docker 但不可用 → runner=None → 不注册(不静默降级)。
    assert app.sandbox_runner is None
    names = _registry_tool_names(app)
    assert "run_python" not in names
