"""docx → PDF 渲染后端 + PDF → 逐页 PNG 栅格化（visual-layout-acceptance · Task 2/3）。

把 `.docx` 渲染成页面图片，供视觉版面验收「看图」。设计：

- **Word_COM_Backend（优先，Windows）**：经 win32com 不可见地驱动 Microsoft Word
  ``ExportAsFixedFormat`` 导出 PDF——保真度即 Word 自身渲染（无保真差）。
- **LibreOffice_Backend（回退）**：``soffice --headless --convert-to pdf``；渲染可能与
  Word 存在差异（尤其浮动体 / 分栏 / 分页），故带保真度告警。
- **rasterize_pdf**：PyMuPDF 逐页转 PNG。

全部**惰性依赖**、**故障隔离**：探测/渲染/栅格化任何失败都不抛，交上层优雅降级。
本模块只读输入 docx，产物写到指定输出目录。
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from typing import Protocol, runtime_checkable


@runtime_checkable
class RenderBackend(Protocol):
    """docx → pdf 渲染后端。"""

    name: str
    fidelity_note: str

    def available(self) -> bool:
        """后端是否可用（惰性探测，绝不抛）。"""
        ...

    def render(self, docx_path: str, out_pdf: str) -> bool:
        """把 docx 渲染为 out_pdf；成功返回 True，任何失败吞并返回 False（不抛）。"""
        ...


class WordComBackend:
    """Windows 上经 COM 自动化驱动 Word 导出 PDF（保真=Word 自身渲染）。"""

    name = "word_com"
    fidelity_note = ""  # Word 自身渲染，无保真差

    def available(self) -> bool:
        if os.name != "nt":
            return False
        try:
            import win32com.client  # noqa: F401,WPS433
        except Exception:  # noqa: BLE001 - 未装 pywin32 / 非 Windows
            return False
        return True

    def render(self, docx_path: str, out_pdf: str) -> bool:
        word = None
        doc = None
        try:
            import pythoncom  # noqa: WPS433
            import win32com.client  # noqa: WPS433

            pythoncom.CoInitialize()
            word = win32com.client.DispatchEx("Word.Application")
            word.Visible = False
            word.DisplayAlerts = 0
            doc = word.Documents.Open(
                os.path.abspath(docx_path), ReadOnly=True, AddToRecentFiles=False
            )
            # 17 = wdExportFormatPDF
            doc.ExportAsFixedFormat(os.path.abspath(out_pdf), 17)
            return os.path.isfile(out_pdf)
        except Exception:  # noqa: BLE001 - 渲染失败 → 交上层降级
            return False
        finally:
            try:
                if doc is not None:
                    doc.Close(False)
            except Exception:  # noqa: BLE001
                pass
            try:
                if word is not None:
                    word.Quit()
            except Exception:  # noqa: BLE001
                pass
            try:
                import pythoncom  # noqa: WPS433

                pythoncom.CoUninitialize()
            except Exception:  # noqa: BLE001
                pass


class LibreOfficeBackend:
    """LibreOffice headless 转 PDF（回退后端，渲染可能与 Word 有差异）。"""

    name = "libreoffice"
    fidelity_note = (
        "本页由 LibreOffice 渲染，与 Microsoft Word 可能存在差异"
        "（尤其在浮动体 / 分栏 / 分页处）；在你的 Word 中不保证完全一致。"
    )

    def __init__(self, soffice_path: str | None = None) -> None:
        self._soffice = soffice_path or os.environ.get("PAPER_SOFFICE_PATH") or ""

    def _resolve(self) -> str | None:
        if self._soffice and os.path.isfile(self._soffice):
            return self._soffice
        for name in ("soffice", "soffice.exe", "libreoffice"):
            found = shutil.which(name)
            if found:
                return found
        return None

    def available(self) -> bool:
        return self._resolve() is not None

    def render(self, docx_path: str, out_pdf: str) -> bool:
        exe = self._resolve()
        if not exe:
            return False
        out_dir = os.path.dirname(os.path.abspath(out_pdf)) or "."
        try:
            os.makedirs(out_dir, exist_ok=True)
            with tempfile.TemporaryDirectory() as profile_dir:
                # 独立 user profile 目录，避免与用户正在运行的 LibreOffice 抢锁。
                subprocess.run(
                    [
                        exe, "--headless", "--convert-to", "pdf",
                        "--outdir", out_dir,
                        f"-env:UserInstallation=file:///{profile_dir.replace(os.sep, '/')}",
                        os.path.abspath(docx_path),
                    ],
                    capture_output=True, timeout=120, shell=False,
                )
            # soffice 以源文件名（.pdf）写出，若与 out_pdf 不一致则搬到目标名。
            produced = os.path.join(
                out_dir, os.path.splitext(os.path.basename(docx_path))[0] + ".pdf"
            )
            if os.path.isfile(produced):
                if os.path.abspath(produced) != os.path.abspath(out_pdf):
                    shutil.move(produced, out_pdf)
                return os.path.isfile(out_pdf)
            return False
        except Exception:  # noqa: BLE001 - 渲染失败 → 交上层降级
            return False


def select_render_backend(soffice_path: str | None = None) -> RenderBackend | None:
    """选择渲染后端：Word 可用优先 Word；否则 LibreOffice；都不可用返回 None。"""
    word = WordComBackend()
    if word.available():
        return word
    libre = LibreOfficeBackend(soffice_path)
    if libre.available():
        return libre
    return None


def rasterize_pdf(pdf_path: str, out_dir: str, *, dpi: int = 150) -> list[str]:
    """PyMuPDF 把 PDF 逐页转 PNG，按页序返回路径列表；不可用/失败返回 []（不抛）。"""
    try:
        import fitz  # PyMuPDF  # noqa: WPS433
    except Exception:  # noqa: BLE001 - 未装 PyMuPDF
        return []
    try:
        os.makedirs(out_dir, exist_ok=True)
        stem = os.path.splitext(os.path.basename(pdf_path))[0]
        zoom = max(0.1, float(dpi) / 72.0)
        matrix = fitz.Matrix(zoom, zoom)
        pages: list[str] = []
        with fitz.open(pdf_path) as doc:
            for i, page in enumerate(doc):
                pix = page.get_pixmap(matrix=matrix)
                out = os.path.join(out_dir, f"{stem}_p{i + 1:03d}.png")
                pix.save(out)
                pages.append(out)
        return pages
    except Exception:  # noqa: BLE001 - 坏 PDF / 渲染异常 → 交上层降级
        return []


def render_docx_to_images(
    docx_path: str, out_dir: str, *, dpi: int = 150, soffice_path: str | None = None
) -> tuple[list[str], str, str]:
    """一步到位：选后端 → 渲染 PDF → 逐页 PNG。

    返回 ``(逐页 png 路径, backend_name, fidelity_note)``；无后端 / 任一步失败 →
    ``([], "", "")``（由上层据空列表优雅降级）。
    """
    backend = select_render_backend(soffice_path)
    if backend is None:
        return [], "", ""
    os.makedirs(out_dir, exist_ok=True)
    pdf_path = os.path.join(
        out_dir, os.path.splitext(os.path.basename(docx_path))[0] + ".pdf"
    )
    if not backend.render(docx_path, pdf_path):
        return [], backend.name, backend.fidelity_note
    images = rasterize_pdf(pdf_path, out_dir, dpi=dpi)
    return images, backend.name, backend.fidelity_note


__all__ = [
    "RenderBackend",
    "WordComBackend",
    "LibreOfficeBackend",
    "select_render_backend",
    "rasterize_pdf",
    "render_docx_to_images",
]
