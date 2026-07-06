"""SubprocessSandbox 单元测试（sandboxed-run-python · Task 1）。

跨平台可测:正常产出文件、非零退出诚实上报、超时被杀、异常隔离、结果截断。
"""

from __future__ import annotations

from paper_agent.agent_platform.sandbox import (
    SandboxResult,
    SandboxRunner,
    SubprocessSandbox,
)


def test_available():
    assert SubprocessSandbox().available() is True


def test_is_runner_protocol():
    assert isinstance(SubprocessSandbox(), SandboxRunner)


def test_normal_run_produces_file(tmp_path):
    code = (
        "with open('out.txt', 'w', encoding='utf-8') as f:\n"
        "    f.write('hello sandbox')\n"
        "print('done')\n"
    )
    result = SubprocessSandbox().run(
        code, str(tmp_path), timeout_s=30, memory_mb=512, allow_network=False
    )
    assert isinstance(result, SandboxResult)
    assert result.ok is True
    assert result.exit_code == 0
    assert "done" in result.stdout
    assert (tmp_path / "out.txt").read_text(encoding="utf-8") == "hello sandbox"


def test_nonzero_exit_is_honest(tmp_path):
    code = "import sys\nsys.stderr.write('boom')\nsys.exit(3)\n"
    result = SubprocessSandbox().run(
        code, str(tmp_path), timeout_s=30, memory_mb=512, allow_network=False
    )
    assert result.ok is False
    assert result.exit_code == 3
    assert "boom" in result.stderr
    assert "非零退出" in result.error


def test_timeout_is_killed(tmp_path):
    code = "import time\ntime.sleep(10)\n"
    result = SubprocessSandbox().run(
        code, str(tmp_path), timeout_s=1, memory_mb=512, allow_network=False
    )
    assert result.ok is False
    assert "超时" in result.error


def test_exception_syntax_error_isolated(tmp_path):
    code = "this is not valid python !!!\n"
    result = SubprocessSandbox().run(
        code, str(tmp_path), timeout_s=30, memory_mb=512, allow_network=False
    )
    # 语法错误 → python 非零退出,被诚实上报,不抛异常。
    assert result.ok is False
    assert result.exit_code not in (0, None)


def test_stdout_truncated(tmp_path):
    code = "print('x' * 20000)\n"
    result = SubprocessSandbox().run(
        code, str(tmp_path), timeout_s=30, memory_mb=512, allow_network=False
    )
    assert result.ok is True
    assert "已截断" in result.stdout
    assert len(result.stdout) < 20000
