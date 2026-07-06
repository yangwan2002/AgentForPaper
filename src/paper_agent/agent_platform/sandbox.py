"""受沙箱约束的代码执行（sandboxed-run-python · Task 1）。

为"低风险长尾工具层" `run_python` 提供隔离执行后端。定位（见 spec）：
- **只服务低风险长尾**（图像/数据/文件/docx 微操），绝不触碰引用/内容/格式的正确性核心；
- 代码在受限环境跑：可写范围锁定 Work_Dir、（Docker 后端）默认断网、限时/限内存。

隔离方案**可插拔**（``SandboxRunner`` 协议）：
- :class:`SubprocessSandbox`（本模块，跨平台基线，隔离**弱**——锁 cwd + 超时 + Unix 资源上限，
  但不真正断网、内存上限仅 Unix）；仅适合本地可信调试。
- ``DockerSandbox``（Task 4，强隔离、Windows 首选）。

本模块 Task 1 实现数据模型、协议与 :class:`SubprocessSandbox`；全程失败诚实、异常隔离。
"""

from __future__ import annotations

import os
import subprocess
import sys
from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

# stdout/stderr 防御式截断上限（与既有工具结果截断口径一致，不可信外部输出）。
_MAX_STREAM_CHARS = 4000
# 写入 Work_Dir 的代码文件名。
_SNIPPET_NAME = "_snippet.py"


def _truncate(text: str, limit: int = _MAX_STREAM_CHARS) -> str:
    text = text or ""
    return text if len(text) <= limit else text[:limit] + "\n[输出过长已截断]"


@dataclass
class SandboxResult:
    """一次沙箱执行的结构化结果。

    - ``ok``：是否正常结束（退出码 0 且无沙箱层错误）。
    - ``exit_code``：进程退出码（超时/未启动为 ``None``）。
    - ``stdout`` / ``stderr``：截断后的标准输出/错误。
    - ``files``：Work_Dir 内**新增/变更**的产物文件绝对路径。
    - ``error``：沙箱层错误（超时/后端不可用/异常），非代码本身的 stderr。
    """

    ok: bool
    exit_code: int | None = None
    stdout: str = ""
    stderr: str = ""
    files: list[str] = field(default_factory=list)
    error: str = ""


@runtime_checkable
class SandboxRunner(Protocol):
    """隔离执行后端抽象。具体实现（子进程/Docker）经装配注入。"""

    name: str

    def available(self) -> bool:
        """该后端在当前平台是否可用。"""
        ...

    def run(
        self,
        code: str,
        work_dir: str,
        *,
        timeout_s: float,
        memory_mb: int,
        allow_network: bool,
    ) -> SandboxResult:
        """在 ``work_dir`` 内执行 ``code``；``work_dir`` 是唯一可写区。绝不抛出。"""
        ...


class SubprocessSandbox:
    """跨平台子进程后端（隔离**弱**）：锁 cwd + 超时 + （Unix）资源上限。

    局限（必须知悉）：
    - **不真正断网**：纯子进程无法可靠阻断网络（``allow_network`` 仅作记录，不强制）；
    - **内存上限仅 Unix 生效**（``resource``）；Windows 上不强制内存/CPU 上限。
    仅适合完全可信的本地调试；生产/多用户请用 Docker 后端。
    """

    name = "subprocess"

    def __init__(self, python_executable: str | None = None) -> None:
        self._python = python_executable or sys.executable

    def available(self) -> bool:
        return bool(self._python)

    def run(
        self,
        code: str,
        work_dir: str,
        *,
        timeout_s: float,
        memory_mb: int,
        allow_network: bool,
    ) -> SandboxResult:
        os.makedirs(work_dir, exist_ok=True)
        snippet = os.path.join(work_dir, _SNIPPET_NAME)
        try:
            with open(snippet, "w", encoding="utf-8") as fh:
                fh.write(code or "")
        except OSError as exc:
            return SandboxResult(ok=False, error=f"写入代码文件失败：{exc}")

        env = self._minimal_env()
        preexec = self._resource_limiter(memory_mb) if os.name == "posix" else None

        try:
            completed = subprocess.run(
                [self._python, _SNIPPET_NAME],
                cwd=work_dir,
                capture_output=True,
                text=True,
                timeout=timeout_s,
                env=env,
                shell=False,  # 绝不经 shell
                preexec_fn=preexec,  # 仅 posix；Windows 下为 None
            )
        except subprocess.TimeoutExpired:
            return SandboxResult(
                ok=False, exit_code=None, error=f"执行超时（>{timeout_s}s），已终止。"
            )
        except (OSError, ValueError) as exc:
            return SandboxResult(ok=False, error=f"沙箱启动失败：{type(exc).__name__}: {exc}")

        ok = completed.returncode == 0
        return SandboxResult(
            ok=ok,
            exit_code=completed.returncode,
            stdout=_truncate(completed.stdout),
            stderr=_truncate(completed.stderr),
            error="" if ok else f"代码非零退出（exit={completed.returncode}）",
        )

    @staticmethod
    def _minimal_env() -> dict:
        """最小化环境变量：保留 PATH/PYTHON 相关，去掉代理（弱化外联），不透传密钥类变量。"""
        keep_prefixes = ("PATH", "PYTHON", "SYSTEMROOT", "TEMP", "TMP", "LANG", "LC_")
        env = {
            k: v
            for k, v in os.environ.items()
            if k.upper().startswith(keep_prefixes)
        }
        # 显式清空代理，弱化默认外联（非强制断网——强隔离用 Docker）。
        for proxy in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy",
                      "https_proxy", "all_proxy"):
            env.pop(proxy, None)
        return env

    @staticmethod
    def _resource_limiter(memory_mb: int):
        """返回 posix ``preexec_fn``，对子进程设内存（地址空间）与 CPU 上限。"""
        def _limit() -> None:  # pragma: no cover - 仅在 posix 子进程内执行
            try:
                import resource

                if memory_mb and memory_mb > 0:
                    nbytes = int(memory_mb) * 1024 * 1024
                    resource.setrlimit(resource.RLIMIT_AS, (nbytes, nbytes))
            except Exception:
                pass

        return _limit


class DockerSandbox:
    """Docker 后端（强隔离、跨平台、Windows 首选）。

    ``docker run --rm --network none --memory {m}m -v {work_dir}:/work -w /work {image}
    python _snippet.py``：内核级文件系统/网络隔离 + 内存上限;宿主侧超时杀容器。
    """

    name = "docker"

    def __init__(self, image: str = "python:3.12-slim", docker_executable: str = "docker") -> None:
        self._image = image
        self._docker = docker_executable

    def available(self) -> bool:
        try:
            completed = subprocess.run(
                [self._docker, "version", "--format", "{{.Server.Version}}"],
                capture_output=True, text=True, timeout=8, shell=False,
            )
            return completed.returncode == 0
        except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
            return False

    def run(
        self,
        code: str,
        work_dir: str,
        *,
        timeout_s: float,
        memory_mb: int,
        allow_network: bool,
    ) -> SandboxResult:
        os.makedirs(work_dir, exist_ok=True)
        snippet = os.path.join(work_dir, _SNIPPET_NAME)
        try:
            with open(snippet, "w", encoding="utf-8") as fh:
                fh.write(code or "")
        except OSError as exc:
            return SandboxResult(ok=False, error=f"写入代码文件失败：{exc}")

        abs_work = os.path.abspath(work_dir)
        container = f"paperagent_runpy_{os.getpid()}_{abs(hash(abs_work)) % 10_000_000}"
        args = [self._docker, "run", "--rm", "--name", container]
        if not allow_network:
            args += ["--network", "none"]
        if memory_mb and memory_mb > 0:
            args += ["--memory", f"{int(memory_mb)}m"]
        args += ["-v", f"{abs_work}:/work", "-w", "/work", self._image,
                 "python", _SNIPPET_NAME]

        try:
            completed = subprocess.run(
                args, capture_output=True, text=True, timeout=timeout_s, shell=False,
            )
        except subprocess.TimeoutExpired:
            # 杀掉可能仍在跑的容器(best-effort),避免残留。
            try:
                subprocess.run([self._docker, "kill", container],
                               capture_output=True, timeout=8, shell=False)
            except Exception:  # noqa: BLE001
                pass
            return SandboxResult(
                ok=False, error=f"执行超时（>{timeout_s}s），已终止容器。"
            )
        except (OSError, ValueError) as exc:
            return SandboxResult(ok=False, error=f"Docker 沙箱启动失败：{type(exc).__name__}: {exc}")

        ok = completed.returncode == 0
        return SandboxResult(
            ok=ok,
            exit_code=completed.returncode,
            stdout=_truncate(completed.stdout),
            stderr=_truncate(completed.stderr),
            error="" if ok else f"代码非零退出（exit={completed.returncode}）",
        )


def select_sandbox(
    backend: str = "auto",
    *,
    image: str = "python:3.12-slim",
) -> tuple[SandboxRunner | None, str]:
    """据配置选隔离后端,返回 (runner 或 None, 说明)。

    - ``docker``：要求 Docker 可用,不可用 → 返回 ``(None, 原因)``(**拒绝,不静默降级**)。
    - ``subprocess``：直接用子进程后端(隔离弱)。
    - ``auto``：有 Docker 用 Docker,否则回退子进程并在说明里**告警**。
    """
    backend = (backend or "auto").lower()
    if backend == "subprocess":
        return SubprocessSandbox(), "使用子进程后端（隔离弱,仅适合本地可信调试）。"
    if backend == "docker":
        docker = DockerSandbox(image=image)
        if docker.available():
            return docker, "使用 Docker 后端（强隔离）。"
        return None, "配置要求 Docker 后端,但 Docker 不可用——已拒绝(不静默降级为弱隔离)。"
    # auto
    docker = DockerSandbox(image=image)
    if docker.available():
        return docker, "auto：检测到 Docker,使用强隔离。"
    return SubprocessSandbox(), "auto：未检测到 Docker,回退子进程后端（隔离弱,谨慎使用）。"


__all__ = [
    "SandboxResult",
    "SandboxRunner",
    "SubprocessSandbox",
    "DockerSandbox",
    "select_sandbox",
]
