"""沙箱后端选择测试（sandboxed-run-python · Task 4）。

Docker 实机用例无 Docker 时跳过;选择策略(拒绝不降级)用桩确定可测。
"""

from __future__ import annotations

from paper_agent.agent_platform import sandbox as sb
from paper_agent.agent_platform.sandbox import (
    DockerSandbox,
    SubprocessSandbox,
    select_sandbox,
)


def test_subprocess_backend_selected():
    runner, note = select_sandbox("subprocess")
    assert isinstance(runner, SubprocessSandbox)
    assert "子进程" in note


def test_docker_unavailable_is_rejected_not_downgraded(monkeypatch):
    # 强制 Docker 不可用 → 指定 docker 应拒绝(返回 None),不静默降级。
    monkeypatch.setattr(DockerSandbox, "available", lambda self: False)
    runner, note = select_sandbox("docker")
    assert runner is None
    assert "拒绝" in note


def test_auto_falls_back_to_subprocess_with_warning(monkeypatch):
    monkeypatch.setattr(DockerSandbox, "available", lambda self: False)
    runner, note = select_sandbox("auto")
    assert isinstance(runner, SubprocessSandbox)
    assert "回退" in note


def test_auto_uses_docker_when_available(monkeypatch):
    monkeypatch.setattr(DockerSandbox, "available", lambda self: True)
    runner, note = select_sandbox("auto")
    assert isinstance(runner, DockerSandbox)
    assert "强隔离" in note


def test_docker_available_probe_no_crash():
    # 只验证 available() 不抛(有无 docker 都应返回 bool)。
    assert isinstance(DockerSandbox().available(), bool)
