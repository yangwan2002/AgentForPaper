"""原子落盘工具：先写临时文件再原子替换，避免崩溃/中断留下半截产物。

导出器直接 ``open(path, "w")`` 时，若写入中途进程崩溃/被杀，用户会拿到一个
**不完整**的 `.md`/`.tex`/`.docx`。本模块提供 tmp-then-rename 语义：内容先写到同目录
下的临时文件，``os.replace`` 在同一文件系统上是原子操作——要么看到旧文件（或无
文件），要么看到完整新文件，绝不会看到半截内容。
"""

from __future__ import annotations

import os
import tempfile
import time


def _replace_with_retry(tmp_path: str, final_path: str) -> None:
    """``os.replace`` + 对瞬时 ``PermissionError`` 有界重试。

    Windows 上杀毒/索引器可能在 ``os.replace`` 瞬间短暂锁住刚写好的临时文件，导致
    ``PermissionError: [WinError 5]``。这类锁是瞬时的——短暂退避后重试即可成功。
    非 Windows 或非该类错误不受影响（首次即成功或如实抛出）。
    """
    delays = (0.05, 0.1, 0.2, 0.4)
    for i in range(len(delays) + 1):
        try:
            os.replace(tmp_path, final_path)
            return
        except PermissionError:
            if i >= len(delays):
                raise
            time.sleep(delays[i])


def atomic_write_text(path: str, text: str, *, encoding: str = "utf-8") -> None:
    """把 ``text`` 原子写入 ``path``（同目录 tmp 文件 + ``os.replace``）。"""
    directory = os.path.dirname(os.path.abspath(path)) or "."
    os.makedirs(directory, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=directory, prefix=".tmp_", suffix=".part")
    try:
        with os.fdopen(fd, "w", encoding=encoding) as fh:
            fh.write(text)
        _replace_with_retry(tmp, path)  # 同一文件系统上原子替换（瞬时锁有界重试）
    except BaseException:
        # 失败：清理临时文件，不留半截产物，向上抛出。
        try:
            if os.path.exists(tmp):
                os.remove(tmp)
        except OSError:
            pass
        raise


def atomic_finalize(tmp_path: str, final_path: str) -> None:
    """把已写好的临时文件 ``tmp_path`` 原子替换为 ``final_path``（用于二进制/外部写入）。

    供 docx 等由第三方库（python-docx / pandoc）写到临时路径后收尾使用。
    对 Windows 瞬时文件锁（``PermissionError``）有界重试。
    """
    _replace_with_retry(tmp_path, final_path)


__all__ = ["atomic_write_text", "atomic_finalize"]
