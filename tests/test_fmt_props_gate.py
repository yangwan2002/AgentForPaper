"""format-pipeline-and-diff-revision Format_Gate 属性测试（Property 17–20）。

每条 Correctness Property 用单个 Hypothesis 属性测试实现（max_examples=100），
直接驱动 ``FormatGate.check`` 的判定与报告组装逻辑（``_decide_passed`` /
片段截断 / 超时记录 / 只读产物）。

外部工具（pandoc / pdflatex）不真实调用：通过在 ``FormatGate`` 实例上替换私有
运行方法（``_pandoc_available`` / ``_run_pandoc_validate`` / ``_pdflatex_available``
/ ``_run_pdflatex``）或替换 ``subprocess.run``，使 ``check()`` 由生成的
``ToolRunResult`` 驱动，从而在无真实二进制、可重复的前提下测试「工具退出码为唯一
裁判」等属性。
"""

from __future__ import annotations

import inspect
import os
import tempfile
from unittest import mock

from hypothesis import given, settings
from hypothesis import strategies as st

from paper_agent.export import format_gate as format_gate_module
from paper_agent.export.format_gate import FormatGate
from paper_agent.export.format_models import (
    FormatGateReport,
    OffendingFragment,
    ToolRunResult,
)
from paper_agent.workspace.models import OutputFormat

_STDERR_MAX = 2000
_EXCERPT_MAX = 500
_MAX_FRAGMENTS = 10


class _LLMTripwire:
    """任何属性访问 / 调用都会引爆的哨兵：证明 Format_Gate 从不触碰 LLM。"""

    def __getattr__(self, name):  # noqa: D401 - tripwire
        raise AssertionError("Format_Gate must not access any LLM")

    def __call__(self, *args, **kwargs):
        raise AssertionError("Format_Gate must not call any LLM")


class _FakeCompleted:
    """模拟 ``subprocess.run`` 的返回对象（不可信外部工具输出）。"""

    def __init__(self, returncode: int, stderr: str = "", stdout: str = "") -> None:
        self.returncode = returncode
        self.stderr = stderr
        self.stdout = stdout


# 单个 (exit_code, timed_out, missing) 生成器，覆盖任意退出码 / 超时 / 缺失组合。
_tool_result_spec = st.tuples(
    st.one_of(st.just(0), st.integers(min_value=-10, max_value=255), st.none()),
    st.booleans(),
    st.booleans(),
)


# --------------------------------------------------------------------------- #
# Property 17
# --------------------------------------------------------------------------- #
# Feature: format-pipeline-and-diff-revision, Property 17: 格式闸以工具退出码为唯一
# 裁判（无 LLM）——passed 当且仅当全部参与工具退出码为 0 且无超时、无缺失工具，全程不调用 LLM。
@settings(max_examples=100)
@given(pandoc_missing=st.booleans(), specs=st.lists(_tool_result_spec, max_size=6))
def test_p17_exit_code_is_sole_judge_no_llm(pandoc_missing, specs):
    # 构造函数签名不得包含任何 llm 参数（格式闸不接受 LLM 依赖）。
    params = inspect.signature(FormatGate.__init__).parameters
    assert "llm" not in params

    results = [
        ToolRunResult(
            tool_name="pandoc",
            exit_code=exit_code,
            timed_out=timed_out,
            missing=missing,
        )
        for (exit_code, timed_out, missing) in specs
    ]

    gate = FormatGate(enable_pdflatex_check=False)
    # 注入哨兵：若 check() 触碰任何 LLM 属性 / 调用，立即失败。
    gate._llm = _LLMTripwire()

    result_iter = iter(results)

    gate._pandoc_available = lambda: not pandoc_missing

    def _fake_validate(path):
        r = next(result_iter)
        return r, [], r.timed_out

    gate._run_pandoc_validate = _fake_validate

    artifact_paths = [f"a{i}.tex" for i in range(len(results))]
    report = gate.check(OutputFormat.LATEX, artifact_paths)

    if pandoc_missing:
        expected = False
        assert "pandoc" in report.missing_tools
    else:
        expected = all(
            r.exit_code == 0 and not r.timed_out and not r.missing for r in results
        )

    assert report.passed is expected


# --------------------------------------------------------------------------- #
# Property 18
# --------------------------------------------------------------------------- #
# Feature: format-pipeline-and-diff-revision, Property 18: 格式闸报告字段有界——失败
# 报告的错误片段 ≤2000 字符、出错定位片段每段 ≤500 且至多 10 段，并含失败工具名与退出码。
@settings(max_examples=100, deadline=None)
@given(
    exit_code=st.integers(min_value=1, max_value=255),
    filler_len=st.integers(min_value=2001, max_value=6000),
    line_numbers=st.lists(
        st.integers(min_value=1, max_value=40), min_size=11, max_size=30, unique=True
    ),
)
def test_p18_failure_report_fields_are_bounded(exit_code, filler_len, line_numbers):
    # 超大 stderr + 多处行号定位，逼近报告字段上限。
    stderr = "E" * filler_len + " " + " ".join(f"line {n}" for n in line_numbers)

    with tempfile.TemporaryDirectory() as tmp:
        tex_path = os.path.join(tmp, "doc.tex")
        # 产物内容含足够多行，使定位片段能取到非空文本。
        with open(tex_path, "w", encoding="utf-8") as fh:
            fh.write("\n".join(f"content line {i} with some text" for i in range(50)))

        gate = FormatGate(enable_pdflatex_check=False)
        gate._pandoc_available = lambda: True

        fake = _FakeCompleted(returncode=exit_code, stderr=stderr, stdout="")
        with mock.patch.object(
            format_gate_module.subprocess, "run", return_value=fake
        ):
            report = gate.check(OutputFormat.LATEX, [tex_path])

    assert report.passed is False

    # 失败工具名 + 退出码存在。
    pandoc_results = [t for t in report.tool_results if t.tool_name == "pandoc"]
    assert pandoc_results, "报告必须含失败的 pandoc 工具结果"
    assert any(t.exit_code == exit_code for t in pandoc_results)

    # stderr 片段有界。
    for t in report.tool_results:
        assert len(t.stderr_excerpt) <= _STDERR_MAX

    # 出错定位片段有界。
    assert len(report.offending_fragments) <= _MAX_FRAGMENTS
    for frag in report.offending_fragments:
        assert isinstance(frag, OffendingFragment)
        assert len(frag.excerpt) <= _EXCERPT_MAX


# --------------------------------------------------------------------------- #
# Property 19
# --------------------------------------------------------------------------- #
# Feature: format-pipeline-and-diff-revision, Property 19: 格式闸超时判负并记录——超时
# 的工具运行使 passed=False，并在报告中记录超时工具名与所用超时阈值 timeout_used_s。
@settings(max_examples=100)
@given(timeout=st.integers(min_value=1, max_value=600))
def test_p19_timeout_fails_and_is_recorded(timeout):
    gate = FormatGate(format_gate_timeout=timeout, enable_pdflatex_check=False)
    gate._pandoc_available = lambda: True

    def _timed_out_validate(path):
        r = ToolRunResult(
            tool_name="pandoc",
            exit_code=None,
            stderr_excerpt="pandoc 运行超时，进程已终止。",
            timed_out=True,
        )
        return r, [], True

    gate._run_pandoc_validate = _timed_out_validate

    report = gate.check(OutputFormat.LATEX, ["doc.tex"])

    assert report.passed is False
    assert report.timeout_used_s == gate._timeout == timeout
    assert any(
        t.timed_out and t.tool_name == "pandoc" for t in report.tool_results
    )


# --------------------------------------------------------------------------- #
# Property 20
# --------------------------------------------------------------------------- #
# Feature: format-pipeline-and-diff-revision, Property 20: 格式闸保留原始产物——passed
# =False 的判定前后产物文件字节级一致且仍存在（Format_Gate 绝不修改/删除产物）。
@settings(max_examples=100, deadline=None)
@given(content=st.binary(max_size=4096), exit_code=st.integers(min_value=1, max_value=255))
def test_p20_preserves_original_artifact(content, exit_code):
    with tempfile.TemporaryDirectory() as tmp:
        tex_path = os.path.join(tmp, "artifact.tex")
        with open(tex_path, "wb") as fh:
            fh.write(content)

        before = open(tex_path, "rb").read()

        gate = FormatGate(enable_pdflatex_check=False)
        gate._pandoc_available = lambda: True

        def _failing_validate(path):
            r = ToolRunResult(
                tool_name="pandoc",
                exit_code=exit_code,
                stderr_excerpt="conversion failed",
            )
            return r, [], False

        gate._run_pandoc_validate = _failing_validate

        report = gate.check(OutputFormat.LATEX, [tex_path])

        assert report.passed is False
        assert os.path.exists(tex_path)
        after = open(tex_path, "rb").read()
        assert after == before
