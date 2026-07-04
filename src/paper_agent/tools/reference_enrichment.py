"""被引文献正文富化（Round 9）：从 ``pdf_url`` 抓取并解析正文，填充 ``full_text``。

引用忠实性审计的 grounding 此前只到 abstract 层——真实论文大量细节声明在被引论文
**正文**，导致大量假阴 ``cannot_verify``。本模块提供把被引文献正文抓来的能力：

- 依赖倒置：``collect_full_texts`` 接受一个可注入的 ``FullTextFetcher``（``fetch(url) ->
  str | None``），纯粹按 ``pdf_url`` 收集正文，**不做工作区写入**（返回 ``{id: text}``
  由调用方经单一写入路径落盘），便于用 stub 测试、与真实网络解耦。
- 默认实现 ``PdfUrlFetcher``：stdlib 下载 PDF 到临时文件，复用既有
  ``ingestion.load_document`` 的 PDF 解析（PyMuPDF/pypdf）取正文；**全程 best-effort**，
  任何失败（网络/超时/非 PDF/缺依赖）都返回 ``None`` 而非抛异常。
- 网络富化默认关闭（``Config.grounding_fulltext_enabled=False``）；开启时对上限条数
  内、有 ``pdf_url`` 且 ``full_text`` 仍为空的已验证文献抓取。
"""

from __future__ import annotations

from typing import Callable, Protocol, runtime_checkable

from paper_agent.workspace.models import ReferenceEntry

# 富化文本的字符上限（防把整篇超长正文塞入工作区/后续 grounding 前）。
_MAX_FULL_TEXT_CHARS = 60000


@runtime_checkable
class FullTextFetcher(Protocol):
    def fetch(self, url: str) -> str | None:
        """抓取并解析 ``url`` 指向的论文正文；失败返回 ``None``（不抛异常）。"""
        ...


def collect_full_texts(
    refs: list[ReferenceEntry],
    fetcher: FullTextFetcher,
    *,
    max_refs: int = 20,
) -> dict[str, str]:
    """为**已验证、有 pdf_url、full_text 仍为空**的文献收集正文，返回 ``{id: full_text}``。

    纯收集：不修改 ``refs``、不写工作区。对每条经注入的 ``fetcher`` 抓取；抓到非空
    文本才纳入结果（截断至上限）。上限 ``max_refs`` 限制网络调用数。任何单条失败
    只跳过该条，不影响其余。
    """
    out: dict[str, str] = {}
    if max_refs <= 0:
        return out
    for ref in refs:
        if len(out) >= max_refs:
            break
        if not getattr(ref, "verified", False):
            continue
        if (getattr(ref, "full_text", "") or "").strip():
            continue  # 已有正文，跳过
        url = (getattr(ref, "pdf_url", "") or "").strip()
        if not url:
            continue
        try:
            text = fetcher.fetch(url)
        except Exception:  # noqa: BLE001 - 富化 best-effort，单条失败不外溢
            text = None
        if text and text.strip():
            out[ref.id] = text.strip()[:_MAX_FULL_TEXT_CHARS]
    return out


class PdfUrlFetcher:
    """默认全文抓取器：下载 PDF → 复用既有 PDF 解析取正文（best-effort）。"""

    def __init__(self, *, timeout_s: float = 15.0) -> None:
        self._timeout = timeout_s

    def fetch(self, url: str) -> str | None:  # pragma: no cover - 依赖网络，stub 测试覆盖逻辑
        import os
        import tempfile
        import urllib.request

        if not url or not url.lower().startswith(("http://", "https://")):
            return None
        tmp_path = None
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "paper-agent/1.0"})
            with urllib.request.urlopen(req, timeout=self._timeout) as resp:
                data = resp.read()
            if not data:
                return None
            fd, tmp_path = tempfile.mkstemp(suffix=".pdf")
            with os.fdopen(fd, "wb") as fh:
                fh.write(data)
            from paper_agent.ingestion import load_document

            return load_document(tmp_path)
        except Exception:  # noqa: BLE001 - 任何失败都返回 None
            return None
        finally:
            if tmp_path:
                try:
                    os.remove(tmp_path)
                except OSError:
                    pass


def build_fetcher(timeout_s: float = 15.0) -> FullTextFetcher:
    """构造默认全文抓取器。"""
    return PdfUrlFetcher(timeout_s=timeout_s)


__all__ = [
    "FullTextFetcher",
    "PdfUrlFetcher",
    "collect_full_texts",
    "build_fetcher",
]
