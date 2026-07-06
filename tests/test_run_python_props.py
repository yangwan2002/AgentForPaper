"""sandboxed-run-python 属性测试（Task 6）。

覆盖:输入原文件不变(Property 2)、失败诚实(Property 7)、超时有界(Property 4)。
用 SubprocessSandbox(跨平台)。
"""

from __future__ import annotations

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from paper_agent.agent_platform.sandbox import SubprocessSandbox

_ALLOW_TMP = [HealthCheck.function_scoped_fixture]


# Property 7: 任意非零退出码都被诚实上报(ok=False + exit_code 一致)。
@settings(max_examples=30, suppress_health_check=_ALLOW_TMP)
@given(exit_code=st.integers(min_value=1, max_value=120))
def test_prop7_nonzero_exit_honest(exit_code, tmp_path):
    code = f"import sys; sys.exit({exit_code})\n"
    r = SubprocessSandbox().run(
        str(code), str(tmp_path), timeout_s=30, memory_mb=256, allow_network=False
    )
    assert r.ok is False
    assert r.exit_code == exit_code


# Property 2: 输入原文件不变(通过工具复制进 Work_Dir,校验在 test_run_python_tool 覆盖工具层)。
# 这里在 sandbox 层验证:sandbox 只写 work_dir,不碰 work_dir 外的文件。
@settings(max_examples=20, deadline=None, suppress_health_check=_ALLOW_TMP)
@given(content=st.text(alphabet="abc 中文123", max_size=40))
def test_prop2_writes_only_in_work_dir(content, tmp_path):
    work = tmp_path / "work"
    work.mkdir(exist_ok=True)
    outside = tmp_path / "outside.txt"
    outside.write_text(content or "x", encoding="utf-8")
    original = outside.read_bytes()

    # 代码在 work_dir 内写文件(相对路径),不触碰 work_dir 外。
    code = "open('made.txt','w').write('inside')\n"
    r = SubprocessSandbox().run(
        code, str(work), timeout_s=30, memory_mb=256, allow_network=False
    )
    assert r.ok is True
    assert (work / "made.txt").exists()
    # work_dir 外的文件不变。
    assert outside.read_bytes() == original


# Property 4: 超时有界终止。
def test_prop4_timeout_bounded(tmp_path):
    r = SubprocessSandbox().run(
        "import time; time.sleep(30)\n", str(tmp_path),
        timeout_s=1, memory_mb=256, allow_network=False,
    )
    assert r.ok is False and "超时" in r.error
