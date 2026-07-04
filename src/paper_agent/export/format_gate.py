"""Format_Gate：确定性格式闸——以实际运行的工具链为格式正确性的唯一裁判。

设计边界（format-pipeline-and-diff-revision，Components/`Format_Gate`）：

- ``check`` 对 LaTeX/docx 产物**实际运行** pandoc 转换校验（真实子进程调用），
  当 ``output_format == LATEX`` 且 ``enable_pdflatex_check`` 且 pdflatex 可用时，
  额外运行 ``pdflatex -interaction=nonstopmode -halt-on-error`` 编译 ``.tex``
  （Req 9.2）。
- 判定规则（Req 9.3）：``passed == all(exit_code == 0 for 参与工具)
  and not any(timed_out) and not missing_tools``。
- 任一非零退出 / 超时 / 工具缺失 → ``passed=False`` + 结构化 ``FormatGateReport``：
  每个工具一条 ``ToolRunResult``（名称/退出码/``stderr_excerpt≤2000``/``timed_out``/
  ``missing``），并从工具 stderr 尽力解析出 ``OffendingFragment``（≤10 段、每段
  ``excerpt≤500``、带行号或字符偏移）（Req 9.4）。
- 超时 → ``passed=False``、终止进程、记录超时工具与 ``timeout_used_s``（Req 9.7）。
- 工具缺失/不可执行 → ``passed=False``、记入 ``missing_tools``（Req 9.8）。
- **绝不调用任何 LLM**（Req 9.5）；与 ``Quality_Gate`` 独立互补（Req 9.6）；
  只读取、绝不修改或删除原始产物文件（Req 9.9）。

所有外部工具输出一律视为不可信数据：用**参数列表** + ``shell=False`` 调用子进程
（防命令注入），并对 stderr / 片段做防御式截断。模块导入不依赖 pandoc/pdflatex
存在——外部程序仅在 ``check`` 被调用时才作为外部系统被惰性调用。

_Requirements: 9.1, 9.2, 9.3, 9.4, 9.5, 9.6, 9.7, 9.8, 9.9_
"""

from __future__ import annotations

import os
import re
import subprocess
import time

from paper_agent.export.format_models import (
    FormatGateReport,
    OffendingFragment,
    ToolRunResult,
)
from paper_agent.export.pandoc_pipeline import PandocConverter
from paper_agent.workspace.models import OutputFormat

# 防御式截断上限（外部工具输出视为不可信数据）。
_STDERR_MAX = 2000
_EXCERPT_MAX = 500
_MAX_FRAGMENTS = 10

# 格式闸超时的合法区间（秒），与 config 校验一致（Req 9.7）。
_TIMEOUT_MIN = 1
_TIMEOUT_MAX = 600
_TIMEOUT_DEFAULT = 60

# 探测外部工具可用性时使用的短超时。
_PROBE_TIMEOUT = 5.0

# 按扩展名映射到 pandoc 的输入格式名（``-f``）。
_SRC_FORMAT_BY_EXT: dict[str, str] = {
    ".tex": "latex",
    ".latex": "latex",
    ".docx": "docx",
}

# 各输出格式期望校验的产物扩展名。
_EXPECTED_EXT: dict[OutputFormat, tuple[str, ...]] = {
    OutputFormat.LATEX: (".tex", ".latex"),
    OutputFormat.DOCX: (".docx",),
}

# 从工具 stderr 里尽力解析行号 / 字符偏移的正则（不可信输入，只读不执行）。
_LINE_PATTERNS = (
    re.compile(r"line[ ]+(\d+)", re.IGNORECASE),      # pandoc: "... (line 5, column 3)"
    re.compile(r"\bl\.(\d+)\b"),                       # TeX: "l.42 ..."
    re.compile(r"on input line[ ]+(\d+)", re.IGNORECASE),
)
_OFFSET_PATTERNS = (
    re.compile(r"offset[ ]+(\d+)", re.IGNORECASE),
    re.compile(r"char(?:acter)?[ ]+(\d+)", re.IGNORECASE),
)


def _truncate(text: str, limit: int) -> str:
    """把不可信文本防御式截断至 ``limit`` 字符。"""

    if not text:
        return ""
    return text if len(text) <= limit else text[:limit]


def _clamp_timeout(value: int) -> int:
    """把 ``format_gate_timeout`` 收敛到 [1, 600]（Req 9.7）。"""

    try:
        v = int(value)
    except (TypeError, ValueError):
        return _TIMEOUT_DEFAULT
    if v < _TIMEOUT_MIN:
        return _TIMEOUT_MIN
    if v > _TIMEOUT_MAX:
        return _TIMEOUT_MAX
    return v


class FormatGate:
    """确定性格式闸：工具退出码是唯一真相，全程不调用 LLM（Req 9.5）。"""

    def __init__(
        self,
        pandoc: PandocConverter | None = None,
        format_gate_timeout: int = _TIMEOUT_DEFAULT,
        enable_pdflatex_check: bool = False,
        pdflatex_executable: str = "pdflatex",
    ) -> None:
        self._pandoc = pandoc if pandoc is not None else PandocConverter()
        self._timeout = _clamp_timeout(format_gate_timeout)
        self._enable_pdflatex_check = bool(enable_pdflatex_check)
        self._pdflatex_executable = pdflatex_executable

    # ------------------------------------------------------------------ #
    # 公共 API
    # ------------------------------------------------------------------ #

    def check(
        self,
        output_format: OutputFormat,
        artifact_paths: list[str],
        sections=None,
    ) -> FormatGateReport:
        """对产物运行工具链并裁定格式正确性（Req 9.1–9.9）。

        - Markdown：不调用任何外部工具，视为平凡通过（Req 7 语义；防御式处理）。
        - LaTeX/docx：实际运行 pandoc 转换校验；LaTeX 且启用且 pdflatex 可用时
          追加 pdflatex 编译校验。
        """

        artifact_paths = list(artifact_paths or [])

        # Markdown 及任何非 LaTeX/docx：不涉及外部工具，平凡通过（防御式）。
        if output_format not in (OutputFormat.LATEX, OutputFormat.DOCX):
            return FormatGateReport(passed=True, output_format=output_format)

        tool_results: list[ToolRunResult] = []
        offending: list[OffendingFragment] = []
        missing_tools: list[str] = []
        timeout_used: int | None = None

        # --- pandoc 转换校验（Req 9.1）---
        if not self._pandoc_available():
            tool_results.append(
                ToolRunResult(
                    tool_name="pandoc",
                    exit_code=None,
                    stderr_excerpt=_truncate(
                        "pandoc 不可用或不可执行（PATH 中未找到或无法运行）。",
                        _STDERR_MAX,
                    ),
                    missing=True,
                )
            )
            missing_tools.append("pandoc")
        else:
            targets = self._select_artifacts(output_format, artifact_paths)
            for path in targets:
                result, frags, timed_out_flag = self._run_pandoc_validate(path)
                tool_results.append(result)
                offending.extend(frags)
                if timed_out_flag:
                    timeout_used = self._timeout

        # --- 可选 pdflatex 编译校验（Req 9.2）---
        if output_format == OutputFormat.LATEX and self._enable_pdflatex_check:
            tex_files = [
                p for p in artifact_paths if os.path.splitext(p)[1].lower() in (".tex", ".latex")
            ]
            if not self._pdflatex_available():
                tool_results.append(
                    ToolRunResult(
                        tool_name="pdflatex",
                        exit_code=None,
                        stderr_excerpt=_truncate(
                            "pdflatex 不可用或不可执行（已启用编译校验但 PATH 中未找到）。",
                            _STDERR_MAX,
                        ),
                        missing=True,
                    )
                )
                missing_tools.append("pdflatex")
            else:
                for path in tex_files:
                    result, frags, timed_out_flag = self._run_pdflatex(path)
                    tool_results.append(result)
                    offending.extend(frags)
                    if timed_out_flag:
                        timeout_used = self._timeout

        # 截断出错片段至最多 10 段（Req 9.4）。
        offending = offending[:_MAX_FRAGMENTS]

        passed = self._decide_passed(tool_results, missing_tools)

        return FormatGateReport(
            passed=passed,
            output_format=output_format,
            tool_results=tool_results,
            offending_fragments=offending,
            timeout_used_s=timeout_used,
            missing_tools=missing_tools,
        )

    def docx_structural_diff_check(self, pre_path: str, post_path: str):
        """docx「结构像不像原文」真校验：比对两份 docx 的语义级结构（计数/标题/sectPr）。

        ``check`` 只能验证「产物本身 pandoc 能否解析」（语法级），查不出「原地润色
        产物是否保持了原文结构」。当存在原文（``pre_path``，如原地润色的输入）时，
        调用此方法比对产物（``post_path``）与原文的结构指纹——段落/表格/图形/超链接/
        脚注引用计数、标题文本集合、分节数——任一不等即判结构被破坏。

        结构判定统一复用 ``export.docx_structural``（单一真相源），对 python-docx
        重序列化的字节噪声鲁棒（不比 part 原始字节）。全程不调用 LLM（Req 9.5）、
        只读两份文件（Req 9.9）。

        返回 ``docx_structural.StructuralDiff``（``ok`` + ``reasons``）。
        """

        from paper_agent.export.docx_structural import docx_structural_diff_check

        return docx_structural_diff_check(pre_path, post_path)

    # ------------------------------------------------------------------ #
    # 判定
    # ------------------------------------------------------------------ #

    @staticmethod
    def _decide_passed(
        tool_results: list[ToolRunResult], missing_tools: list[str]
    ) -> bool:
        """``passed == all(exit_code==0) and not any(timed_out) and not missing``。"""

        if missing_tools:
            return False
        if any(t.timed_out for t in tool_results):
            return False
        for t in tool_results:
            if t.missing or t.timed_out:
                return False
            if t.exit_code != 0:
                return False
        return True

    # ------------------------------------------------------------------ #
    # 工具可用性探测（不调用 LLM，Req 9.8）
    # ------------------------------------------------------------------ #

    def _pandoc_available(self) -> bool:
        """经注入的 PandocConverter 探测 pandoc 是否可用（Req 9.8）。"""

        try:
            return bool(self._pandoc.probe(timeout=min(_PROBE_TIMEOUT, float(self._timeout))))
        except Exception:
            return False

    def _pandoc_executable(self) -> str:
        """取 PandocConverter 使用的可执行名（缺省 ``pandoc``）。"""

        return getattr(self._pandoc, "_executable", "pandoc")

    def _pdflatex_available(self) -> bool:
        """探测 pdflatex 是否可执行（``pdflatex --version`` 退出码 0）。"""

        try:
            completed = subprocess.run(
                [self._pdflatex_executable, "--version"],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",  # 工具输出不可信，避免 locale 解码崩溃
                timeout=min(_PROBE_TIMEOUT, float(self._timeout)),
                shell=False,  # 绝不经 shell（防注入）
            )
            return completed.returncode == 0
        except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
            return False

    # ------------------------------------------------------------------ #
    # 产物选择
    # ------------------------------------------------------------------ #

    @staticmethod
    def _select_artifacts(
        output_format: OutputFormat, artifact_paths: list[str]
    ) -> list[str]:
        """挑出与输出格式匹配扩展名的产物；无匹配时回退到全部产物。"""

        expected = _EXPECTED_EXT.get(output_format, ())
        matched = [
            p for p in artifact_paths if os.path.splitext(p)[1].lower() in expected
        ]
        return matched if matched else list(artifact_paths)

    # ------------------------------------------------------------------ #
    # 子进程运行（参数列表 + shell=False，Req 9.1/9.2/9.7）
    # ------------------------------------------------------------------ #

    def _run_pandoc_validate(
        self, artifact_path: str
    ) -> tuple[ToolRunResult, list[OffendingFragment], bool]:
        """用 pandoc 读取产物并转 ``native`` 做解析校验（不写文件、只读产物）。"""

        ext = os.path.splitext(artifact_path)[1].lower()
        src_format = _SRC_FORMAT_BY_EXT.get(ext)
        args = [self._pandoc_executable()]
        if src_format:
            args += ["-f", src_format]
        # 转 native 到 stdout（不带 -o，绝不写/改产物，Req 9.9）。
        args += ["-t", "native", artifact_path]

        return self._run_tool("pandoc", args, artifact_path, cwd=None)

    def _run_pdflatex(
        self, tex_path: str
    ) -> tuple[ToolRunResult, list[OffendingFragment], bool]:
        """在 ``.tex`` 所在目录运行非交互 pdflatex 编译校验（Req 9.2）。"""

        directory = os.path.dirname(os.path.abspath(tex_path)) or None
        basename = os.path.basename(tex_path)
        args = [
            self._pdflatex_executable,
            "-interaction=nonstopmode",
            "-halt-on-error",
            basename,
        ]
        return self._run_tool("pdflatex", args, tex_path, cwd=directory)

    def _run_tool(
        self,
        tool_name: str,
        args: list[str],
        artifact_path: str,
        cwd: str | None,
    ) -> tuple[ToolRunResult, list[OffendingFragment], bool]:
        """执行单个外部工具，捕获退出码/stderr，处理超时与缺失（Req 9.4/9.7/9.8）。"""

        start = time.monotonic()
        try:
            completed = subprocess.run(
                args,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",  # 工具输出不可信，避免 locale 解码崩溃
                timeout=self._timeout,
                shell=False,  # 绝不经 shell（防注入）
                cwd=cwd,
            )
        except subprocess.TimeoutExpired as exc:
            # 超时：subprocess.run 已终止子进程（Req 9.7）。
            duration = time.monotonic() - start
            stderr = ""
            if getattr(exc, "stderr", None):
                stderr = exc.stderr.decode() if isinstance(exc.stderr, bytes) else str(exc.stderr)
            result = ToolRunResult(
                tool_name=tool_name,
                exit_code=None,
                stderr_excerpt=_truncate(
                    stderr or f"{tool_name} 运行超过 {self._timeout}s 超时上限，进程已终止。",
                    _STDERR_MAX,
                ),
                duration_s=duration,
                timed_out=True,
            )
            return result, [], True
        except (FileNotFoundError, OSError) as exc:
            # 运行时才发现不可执行：记为缺失工具（Req 9.8）。
            duration = time.monotonic() - start
            result = ToolRunResult(
                tool_name=tool_name,
                exit_code=None,
                stderr_excerpt=_truncate(f"{type(exc).__name__}: {exc}", _STDERR_MAX),
                duration_s=duration,
                missing=True,
            )
            return result, [], False

        duration = time.monotonic() - start
        stderr = _truncate(completed.stderr or "", _STDERR_MAX)
        result = ToolRunResult(
            tool_name=tool_name,
            exit_code=completed.returncode,
            stderr_excerpt=stderr,
            duration_s=duration,
        )

        fragments: list[OffendingFragment] = []
        if completed.returncode != 0:
            fragments = self._extract_offending_fragments(
                completed.stderr or "", artifact_path
            )
        return result, fragments, False

    # ------------------------------------------------------------------ #
    # 出错片段解析（尽力从 stderr 定位行号/偏移，Req 9.4）
    # ------------------------------------------------------------------ #

    def _extract_offending_fragments(
        self, stderr: str, artifact_path: str
    ) -> list[OffendingFragment]:
        """从工具 stderr 解析行号/字符偏移，并从产物取对应片段（≤10 段、≤500 字符）。"""

        if not stderr:
            return []

        # 只读取产物内容用于定位；读失败不抛异常（Req 9.9 只读）。
        content = self._read_artifact(artifact_path)
        lines = content.splitlines() if content else []

        fragments: list[OffendingFragment] = []
        seen_lines: set[int] = set()
        seen_offsets: set[int] = set()

        # 1) 行号定位。
        for pat in _LINE_PATTERNS:
            for m in pat.finditer(stderr):
                if len(fragments) >= _MAX_FRAGMENTS:
                    break
                lineno = int(m.group(1))
                if lineno in seen_lines:
                    continue
                seen_lines.add(lineno)
                excerpt = ""
                if 1 <= lineno <= len(lines):
                    excerpt = _truncate(lines[lineno - 1], _EXCERPT_MAX)
                fragments.append(
                    OffendingFragment(
                        section_id=None,
                        location=f"line {lineno}",
                        excerpt=excerpt,
                    )
                )

        # 2) 字符偏移定位。
        for pat in _OFFSET_PATTERNS:
            for m in pat.finditer(stderr):
                if len(fragments) >= _MAX_FRAGMENTS:
                    break
                offset = int(m.group(1))
                if offset in seen_offsets:
                    continue
                seen_offsets.add(offset)
                excerpt = ""
                if content and 0 <= offset < len(content):
                    excerpt = _truncate(content[offset : offset + _EXCERPT_MAX], _EXCERPT_MAX)
                fragments.append(
                    OffendingFragment(
                        section_id=None,
                        location=f"offset {offset}",
                        excerpt=excerpt,
                    )
                )

        # 3) 无法解析到任何位置时，回退给出一段 stderr 摘要，保证报告可诊断。
        if not fragments:
            fragments.append(
                OffendingFragment(
                    section_id=None,
                    location="unlocated",
                    excerpt=_truncate(stderr.strip(), _EXCERPT_MAX),
                )
            )

        return fragments[:_MAX_FRAGMENTS]

    @staticmethod
    def _read_artifact(artifact_path: str) -> str:
        """只读读取产物文本用于定位；任何失败静默返回空串（绝不修改/删除，Req 9.9）。"""

        try:
            with open(artifact_path, "r", encoding="utf-8", errors="replace") as fh:
                return fh.read()
        except (OSError, ValueError):
            return ""
