"""PandocConverter：pandoc 探测与 Markdown→LaTeX/docx 片段转换。

设计边界（format-pipeline-and-diff-revision，Components/PandocConverter）：
- ``probe``：探测 pandoc 可执行且能返回版本号；超时/未返回版本 → 判不可用（Req 8.1）。
- ``convert``：用 **参数列表**（非 shell 字符串）调用 ``subprocess``，避免命令注入
  （Req 12.9）；非零退出或异常 → ``ConversionResult(ok=False, exit_code, stderr≤2000)``
  （Req 6.4）；外部工具输出一律视为不可信数据，防御式截断至 ≤2000 字符。

模块导入不依赖 pandoc 存在：pandoc 仅在 ``probe`` / ``convert`` 被调用时才作为
外部系统可执行程序被惰性调用（Req 6.1）。导入本模块在无 pandoc 环境下必须成功。

_Requirements: 6.1, 6.4, 8.1, 12.9_
"""

from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass

# 外部工具输出（stdout/stderr）视为不可信数据，防御式截断上限。
_STDERR_MAX = 2000

# pandoc target -> 传给 ``-t`` 的输出格式名。
_TARGET_FORMATS: dict[str, str] = {
    "latex": "latex",
    "docx": "docx",
}


def _truncate(text: str, limit: int = _STDERR_MAX) -> str:
    """把不可信文本防御式截断至 ``limit`` 字符。"""

    if not text:
        return ""
    if len(text) <= limit:
        return text
    return text[:limit]


@dataclass
class ConversionResult:
    """pandoc 单次转换结果。

    - ``ok``：转换是否成功（退出码 0 且无异常）。
    - ``exit_code``：pandoc 进程退出码；异常/未运行时为 ``None``。
    - ``stderr``：错误消息片段，≤2000 字符（不可信数据，已截断）。
    - ``output_path``：docx 等写文件目标时的产物路径。
    - ``content``：latex 等经 stdout 捕获的转换文本。
    """

    ok: bool
    exit_code: int | None
    stderr: str = ""
    output_path: str | None = None
    content: str = ""


class PandocConverter:
    """惰性调用 pandoc 的转换器；导入时不要求 pandoc 存在。"""

    def __init__(self, executable: str | None = None) -> None:
        # 可执行路径解析优先级：显式参数 > 环境变量 PANDOC_PATH > PATH 中的 "pandoc"。
        # 生产不假设 pandoc 在 PATH：装在任意目录时设 PANDOC_PATH 指向 pandoc.exe 即可。
        self._executable = executable or os.environ.get("PANDOC_PATH") or "pandoc"
        self._probe_cache: bool | None = None

    def probe(self, timeout: float = 5.0) -> bool:
        """探测 pandoc 是否可用（可执行且能返回版本号）。

        仅当 pandoc 在 ``timeout`` 内以退出码 0 返回非空版本字符串时判可用；
        任何 ``FileNotFoundError`` / ``TimeoutExpired`` / ``OSError`` → 判不可用
        （Req 8.1）。结果被缓存以避免重复探测。
        """

        if self._probe_cache is not None:
            return self._probe_cache

        available = False
        try:
            completed = subprocess.run(
                [self._executable, "--version"],
                capture_output=True,
                text=True,
                encoding="utf-8",  # 固定 UTF-8，勿用 Windows 默认 GBK（否则中文损坏）
                errors="replace",
                timeout=timeout,
                shell=False,  # 绝不经 shell（Req 12.9）
            )
            available = completed.returncode == 0 and bool(
                (completed.stdout or "").strip()
            )
        except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
            available = False

        self._probe_cache = available
        return available

    def convert(
        self,
        markdown: str,
        target: str,
        out_path: str | None = None,
        timeout: float = 60.0,
    ) -> ConversionResult:
        """把 Normalized_Markdown 转换为 ``target`` 格式。

        ``target`` ∈ ``{"latex", "docx"}``。使用 **参数列表** 调用 pandoc，
        markdown 经 stdin 送入（Req 12.9）：
        - ``latex``：stdout 捕获为 ``content``。
        - ``docx``：写入 ``out_path``（二进制产物无法走 stdout）。

        非零退出或任何异常 → ``ConversionResult(ok=False, exit_code, stderr≤2000)``
        （Req 6.4）。所有输出视为不可信数据并防御式截断。
        """

        out_format = _TARGET_FORMATS.get(target)
        if out_format is None:
            return ConversionResult(
                ok=False,
                exit_code=None,
                stderr=_truncate(
                    f"unsupported target: {target!r}; expected one of "
                    f"{sorted(_TARGET_FORMATS)}"
                ),
            )

        if target == "docx" and not out_path:
            return ConversionResult(
                ok=False,
                exit_code=None,
                stderr=_truncate("docx target requires out_path"),
            )

        args = [self._executable, "-f", "markdown", "-t", out_format]
        if out_path:
            args += ["-o", out_path]

        try:
            completed = subprocess.run(
                args,
                input=markdown,
                capture_output=True,
                text=True,
                encoding="utf-8",  # 关键：中文 markdown 经 stdin 必须按 UTF-8 编码，
                errors="replace",  # 否则 Windows 默认 GBK 会使 pandoc 产出乱码 docx
                timeout=timeout,
                shell=False,  # 绝不经 shell（Req 12.9）
            )
        except (FileNotFoundError, subprocess.TimeoutExpired, OSError) as exc:
            return ConversionResult(
                ok=False,
                exit_code=None,
                stderr=_truncate(f"{type(exc).__name__}: {exc}"),
            )

        stderr = _truncate(completed.stderr or "")
        if completed.returncode != 0:
            return ConversionResult(
                ok=False,
                exit_code=completed.returncode,
                stderr=stderr,
                output_path=out_path,
            )

        return ConversionResult(
            ok=True,
            exit_code=0,
            stderr=stderr,
            output_path=out_path,
            content=completed.stdout or "" if out_path is None else "",
        )

    def convert_file(
        self,
        in_path: str,
        out_path: str,
        *,
        from_format: str,
        to_format: str,
        extra_args: list[str] | None = None,
        timeout: float = 180.0,
    ) -> ConversionResult:
        """文件到文件的**跨格式直转**（如 LaTeX 源 → docx，保留公式/结构）。

        与 ``convert``（Markdown 经 stdin）不同：本方法直接以 ``in_path`` 为输入、
        ``-f from_format -t to_format -o out_path``，适合"用户原 .tex → docx"这类
        跨格式转换——公式、章节结构由 pandoc 正确映射，而非当纯文本重建。

        用参数列表调用、绝不经 shell（防注入）；非零退出/异常 → ``ok=False`` 且
        stderr 截断。``--resource-path`` 设为源文件目录，使 ``\\input`` / 图片相对
        路径可解析。
        """
        import os

        args = [
            self._executable,
            "-f", from_format,
            "-t", to_format,
            "-o", out_path,
        ]
        resource_dir = os.path.dirname(os.path.abspath(in_path)) or "."
        args += ["--resource-path", resource_dir]
        if extra_args:
            args += list(extra_args)
        args.append(in_path)

        try:
            completed = subprocess.run(
                args,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout,
                shell=False,
            )
        except (FileNotFoundError, subprocess.TimeoutExpired, OSError) as exc:
            return ConversionResult(
                ok=False, exit_code=None, stderr=_truncate(f"{type(exc).__name__}: {exc}")
            )

        stderr = _truncate(completed.stderr or "")
        if completed.returncode != 0:
            return ConversionResult(
                ok=False, exit_code=completed.returncode, stderr=stderr,
                output_path=out_path,
            )
        return ConversionResult(
            ok=True, exit_code=0, stderr=stderr, output_path=out_path
        )
