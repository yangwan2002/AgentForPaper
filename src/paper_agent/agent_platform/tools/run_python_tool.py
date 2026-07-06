"""run_python 工具：低风险长尾的受沙箱通用代码执行（sandboxed-run-python · Task 2/3）。

定位（见 spec）：让智能体写一小段 Python（预装 Pillow/matplotlib/pandas/python-docx/PyPDF）在
**隔离沙箱**里跑,覆盖"拼图/裁剪/缩放/画图/合并 PDF/docx 段落微调"等**低风险长尾**——替代
"一个需求一个窄工具"。

安全契约:
- **绝不触碰正确性核心**:本工具**不持有 repo/gate 写能力**,沙箱内代码改不了工作区
  ``section_drafts``/``verified_references``;改章节/引用/忠实性/保格式转换走既有受控工具。
- **输入只读**:输入文件复制进 Work_Dir,原文件字节不变。
- **docx 微操走副本 + 无损校验**(Task 3):产物 docx 相对同名输入 docx 过 ``Preservation_Check``,
  破坏原有结构即判失败、保留原稿。
"""

from __future__ import annotations

import os
import shutil
import tempfile

from paper_agent.agent_platform.sandbox import SandboxRunner
from paper_agent.tools.registry import ToolRegistry

_SNIPPET_NAME = "_snippet.py"

_SCHEMA = {
    "type": "object",
    "properties": {
        "code": {
            "type": "string",
            "description": (
                "要执行的 Python 源码。可 import Pillow(PIL)/matplotlib/pandas/docx/pypdf 等；"
                "只能读写当前工作目录(输入文件已复制到此)。产出的新文件会作为结果返回。"
            ),
        },
        "input_files": {
            "type": "array",
            "items": {"type": "string"},
            "description": "需要读取的输入文件绝对路径(会只读复制进工作目录,原文件不动)。",
        },
        "preserve_docx": {
            "type": "array",
            "items": {"type": "string"},
            "description": (
                "声明这些输出 docx 文件名应相对同名输入 docx **保结构**(触发无损校验)。"
                "改坏原有段落/表格/公式即判失败、保留原稿。"
            ),
        },
        "timeout_s": {"type": "number", "description": "超时秒数(不超过系统上限)。"},
        "memory_mb": {"type": "integer", "description": "内存上限 MB(不超过系统上限)。"},
        "allow_network": {"type": "boolean", "description": "是否允许联网(默认否)。"},
    },
    "required": ["code"],
}

_DESCRIPTION = (
    "在隔离沙箱里执行一小段 Python,用于**低风险长尾**操作:拼接/裁剪/缩放图片、把图插入 docx、"
    "给某段设悬挂缩进、合并/拆分 PDF、从数据画统计图等。预装 Pillow/matplotlib/pandas/"
    "python-docx/pypdf。只能读写工作目录(输入文件已复制进来)、默认断网、限时。**不要**用它改"
    "章节内容、加/核验引用、做保格式转换或就地增补——那些走各自的专用工具。"
)


def _snapshot(work_dir: str) -> dict[str, tuple[int, float]]:
    """记录目录内文件的 (size, mtime),用于识别执行后新增/变更的产物。"""
    snap: dict[str, tuple[int, float]] = {}
    for root, _dirs, files in os.walk(work_dir):
        for name in files:
            p = os.path.join(root, name)
            try:
                st = os.stat(p)
                snap[p] = (st.st_size, st.st_mtime)
            except OSError:
                continue
    return snap


def _collect_products(work_dir: str, before: dict[str, tuple[int, float]]) -> list[str]:
    """收集执行后新增/变更的文件(排除代码片段本身)。"""
    products: list[str] = []
    for root, _dirs, files in os.walk(work_dir):
        for name in files:
            if name == _SNIPPET_NAME:
                continue
            p = os.path.join(root, name)
            try:
                st = os.stat(p)
            except OSError:
                continue
            sig = (st.st_size, st.st_mtime)
            if before.get(p) != sig:
                products.append(p)
    return sorted(products)


def _handle_run_python(
    ctx,
    runner: SandboxRunner,
    *,
    default_timeout_s: float,
    default_memory_mb: int,
    code: str,
    input_files=None,
    preserve_docx=None,
    timeout_s=None,
    memory_mb=None,
    allow_network: bool = False,
) -> str:
    if not (code or "").strip():
        return "未提供要执行的代码。"
    if not runner.available():
        return f"沙箱后端({runner.name})不可用,已拒绝执行(不裸跑无隔离)。"

    # 上限收窄:调用方可比默认更小,但不可超过系统默认(防放大攻击面)。
    eff_timeout = min(float(timeout_s), default_timeout_s) if timeout_s else default_timeout_s
    eff_memory = min(int(memory_mb), default_memory_mb) if memory_mb else default_memory_mb

    output_dir = getattr(ctx, "output_dir", "output")
    os.makedirs(output_dir, exist_ok=True)
    work_dir = tempfile.mkdtemp(prefix="_runpy_", dir=output_dir)

    # 输入文件只读复制进 Work_Dir(原文件字节不变,Property 2)。
    input_map: dict[str, str] = {}  # basename -> 原路径(供 docx 保结构校验用)
    for src in input_files or []:
        src = str(src).strip().strip('"').strip("'")
        if src and os.path.isfile(src):
            base = os.path.basename(src)
            try:
                shutil.copyfile(src, os.path.join(work_dir, base))
                input_map[base] = src
            except OSError:
                pass

    before = _snapshot(work_dir)
    result = runner.run(
        code, work_dir,
        timeout_s=eff_timeout, memory_mb=eff_memory, allow_network=bool(allow_network),
    )
    products = _collect_products(work_dir, before)

    # docx 微操无损校验(Task 3):对声明保结构的产物 docx 比对同名输入 docx。
    unresolved = _check_docx_preservation(products, input_map, preserve_docx or [])

    ctx.session.record(
        "run_python", backend=runner.name, ok=result.ok and not unresolved,
        files=products,
    )
    return _format(result, products, unresolved)


def _check_docx_preservation(products, input_map, preserve_docx) -> list[str]:
    """对产物中的 docx,若声明保结构或有同名输入 docx,则复用 Preservation_Check。

    返回未通过项说明列表(空=全通过)。破坏结构的产物会被删除,不交付破坏性文件。
    """
    from paper_agent.inplace_augment import _preservation_check_docx  # 见 Task 3 接口

    declared = {os.path.basename(str(p)) for p in preserve_docx}
    unresolved: list[str] = []
    for prod in list(products):
        base = os.path.basename(prod)
        if not base.lower().endswith(".docx"):
            continue
        original = input_map.get(base)
        if original is None and base not in declared:
            continue  # 纯新产物、无同名输入 → 不强制校验
        if original is None:
            continue  # 声明了但没有对应输入,无从比对,跳过
        ok, reason = _preservation_check_docx(original, prod)
        if not ok:
            unresolved.append(f"{base}: {reason}")
            try:
                os.remove(prod)  # 删破坏性产物,不交付
                products.remove(prod)
            except (OSError, ValueError):
                pass
    return unresolved


def _format(result, products: list[str], unresolved: list[str]) -> str:
    parts: list[str] = []
    if unresolved:
        parts.append("docx 保结构校验未通过(已保留原稿、丢弃破坏性产物):")
        parts.extend(f"  - {u}" for u in unresolved)
    if result.ok and not unresolved:
        parts.append("代码执行成功。")
    elif not result.ok:
        parts.append(result.error or "代码执行失败。")
    if products:
        parts.append("产出文件:\n" + "\n".join(f"  - {p}" for p in products))
    if result.stdout:
        parts.append("stdout:\n" + result.stdout)
    if result.stderr and not result.ok:
        parts.append("stderr:\n" + result.stderr)
    return "\n".join(parts) if parts else "(无输出)"


def register_run_python(
    registry: ToolRegistry,
    ctx,
    runner: SandboxRunner,
    *,
    default_timeout_s: float = 30.0,
    default_memory_mb: int = 512,
) -> None:
    """把 run_python 工具注册进 registry(隔离后端经 ``runner`` 注入)。"""
    registry.register(
        name="run_python",
        description=_DESCRIPTION,
        handler=lambda code, input_files=None, preserve_docx=None, timeout_s=None, memory_mb=None, allow_network=False: (  # noqa: E501
            _handle_run_python(
                ctx, runner,
                default_timeout_s=default_timeout_s, default_memory_mb=default_memory_mb,
                code=code, input_files=input_files, preserve_docx=preserve_docx,
                timeout_s=timeout_s, memory_mb=memory_mb, allow_network=allow_network,
            )
        ),
        parameters=_SCHEMA,
    )


__all__ = ["register_run_python"]
