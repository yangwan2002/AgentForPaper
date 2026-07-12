"""文档加载器：按扩展名把文件加载为（富）纯文本/Markdown。

设计为可插拔注册表：新增格式 = 注册一个 (扩展名 → 加载函数)。
重依赖（PyMuPDF / pypdf / python-docx）惰性导入，保持核心零依赖。

解析能力与限制（务实说明）：
- 正文文字：PyMuPDF 按阅读顺序抽取，质量较高。
- 表格：检测后转为 Markdown 表格，**表格内的数值得以精确保留**。
- 图片：可抽出为文件（需提供 asset_dir），正文中保留占位与图注引用；
  但"图中绘制的数据"无法从栅格图反推（需原始数据或视觉模型）。
- 扫描件/图片型 PDF：无文本层，需 OCR，暂不支持。
"""

from __future__ import annotations

import os
from typing import Callable

from paper_agent.ingestion.quality import (
    IngestionQualityReport,
    assess_ingestion_quality,
)
from paper_agent.ingestion.sections import normalize_extracted_text


class DocumentLoadError(Exception):
    """文档加载失败（不支持的格式、缺依赖、解析错误）。"""


class IngestionConfirmationRequired(DocumentLoadError):
    """文档正文可读但质量边缘，调用方必须显式确认后才能继续。"""

    def __init__(self, path: str, report: IngestionQualityReport) -> None:
        self.path = path
        self.report = report
        warnings = "；".join(report.warnings)
        super().__init__(
            f"文档摄入需要确认（评分 {report.score}/100）：{warnings}"
        )


def _load_text(path: str, asset_dir: str | None = None) -> str:
    with open(path, "r", encoding="utf-8-sig") as fh:  # 兼容 BOM
        return fh.read()


# --- PDF ---

def _load_pdf(path: str, asset_dir: str | None = None) -> str:
    """优先用 PyMuPDF（强）：正文 + 表格(Markdown) + 图片抽取；
    PyMuPDF 不可用时回退到 pypdf（仅正文）。"""
    text = _load_pdf_pymupdf(path, asset_dir)
    if text is not None:
        return text
    return _load_pdf_pypdf(path)


def _load_pdf_pymupdf(path: str, asset_dir: str | None) -> str | None:
    try:
        import fitz  # PyMuPDF  # noqa: WPS433
    except ImportError:
        return None
    try:
        doc = fitz.open(path)
    except Exception as exc:  # pragma: no cover - 取决于文件
        raise DocumentLoadError(f"PDF 打开失败：{exc}") from exc

    parts: list[str] = []
    img_count = 0
    for pno, page in enumerate(doc, start=1):
        page_text = page.get_text("text").strip()
        if page_text:
            parts.append(page_text)

        # 表格 → Markdown（保留数值）。
        try:
            tables = page.find_tables()
            for ti, table in enumerate(getattr(tables, "tables", []), start=1):
                md = table.to_markdown()
                if md and md.strip():
                    parts.append(f"\n[表格 第{pno}页 #{ti}]\n{md.strip()}\n")
        except Exception:  # pragma: no cover - 表格检测尽力而为
            pass

        # 图片：可选抽取到 asset_dir，并在正文留占位。
        try:
            images = page.get_images(full=True)
        except Exception:  # pragma: no cover
            images = []
        for _ in images:
            img_count += 1
        if images:
            parts.append(f"\n[图 第{pno}页：{len(images)} 张图片]\n")

    if asset_dir and img_count:
        _extract_pdf_images(doc, asset_dir)

    doc.close()
    text = "\n\n".join(parts).strip()
    if not text:
        raise DocumentLoadError(
            "PDF 未能抽取到文本（可能是扫描件/图片型 PDF，需 OCR，暂不支持）。"
        )
    if img_count:
        text += (
            f"\n\n> 注：本文档含 {img_count} 张图片。图中绘制的数据无法从图像自动还原，"
            f"如需精确使用请单独提供原始数据。"
        )
    return text


def _extract_pdf_images(doc, asset_dir: str) -> None:  # pragma: no cover - IO
    import fitz  # noqa: WPS433

    os.makedirs(asset_dir, exist_ok=True)
    seen: set[int] = set()
    for pno, page in enumerate(doc, start=1):
        for img in page.get_images(full=True):
            xref = img[0]
            if xref in seen:
                continue
            seen.add(xref)
            try:
                pix = fitz.Pixmap(doc, xref)
                if pix.n - pix.alpha >= 4:  # CMYK → RGB
                    pix = fitz.Pixmap(fitz.csRGB, pix)
                out = os.path.join(asset_dir, f"p{pno}_x{xref}.png")
                pix.save(out)
            except Exception:
                continue


def _load_pdf_pypdf(path: str) -> str:
    try:
        from pypdf import PdfReader  # noqa: WPS433
    except ImportError as exc:  # pragma: no cover - 环境相关
        raise DocumentLoadError(
            "解析 PDF 需要 PyMuPDF 或 pypdf：pip install '.[pdf]'"
        ) from exc
    try:
        reader = PdfReader(path)
        pages = [page.extract_text() or "" for page in reader.pages]
    except Exception as exc:  # pragma: no cover - 取决于文件
        raise DocumentLoadError(f"PDF 解析失败：{exc}") from exc
    text = "\n".join(pages).strip()
    if not text:
        raise DocumentLoadError(
            "PDF 未能抽取到文本（可能是扫描件/图片型 PDF，需 OCR，暂不支持）。"
        )
    return text


# --- DOCX ---

def _load_docx(path: str, asset_dir: str | None = None) -> str:
    """按文档顺序抽取段落与表格（表格转 Markdown，保留数值）。"""
    try:
        import docx  # noqa: WPS433
        from docx.document import Document as _Doc  # noqa: WPS433
        from docx.table import Table  # noqa: WPS433
        from docx.text.paragraph import Paragraph  # noqa: WPS433
    except ImportError as exc:  # pragma: no cover - 环境相关
        raise DocumentLoadError(
            "解析 docx 需要 python-docx：pip install '.[docx]'"
        ) from exc
    try:
        document = docx.Document(path)
    except Exception as exc:  # pragma: no cover
        raise DocumentLoadError(f"docx 解析失败：{exc}") from exc

    parts: list[str] = []
    body = document.element.body
    for child in body.iterchildren():
        if child.tag.endswith("}p"):
            text = Paragraph(child, document).text.strip()
            if text:
                parts.append(text)
        elif child.tag.endswith("}tbl"):
            table = Table(child, document)
            parts.append(_docx_table_to_markdown(table))
    return "\n\n".join(p for p in parts if p).strip()


def _docx_table_to_markdown(table) -> str:
    rows = []
    for row in table.rows:
        rows.append([cell.text.strip().replace("\n", " ") for cell in row.cells])
    if not rows:
        return ""
    header = rows[0]
    lines = ["| " + " | ".join(header) + " |"]
    lines.append("| " + " | ".join("---" for _ in header) + " |")
    for r in rows[1:]:
        lines.append("| " + " | ".join(r) + " |")
    return "\n".join(lines)


# 扩展名 → 加载函数。新增格式只需在此注册一行。
_LOADERS: dict[str, Callable[..., str]] = {
    ".txt": _load_text,
    ".md": _load_text,
    ".markdown": _load_text,
    ".text": _load_text,
    # LaTeX 源码按纯文本读取（保留字节原样）；章节切分器支持 \section{} 标题，
    # 故 .tex 初稿能按章节切分做局部修订——优于 PDF（PDF 抽取会丢失标题层级）。
    ".tex": _load_text,
    ".latex": _load_text,
    ".pdf": _load_pdf,
    ".docx": _load_docx,
}


def supported_extensions() -> list[str]:
    return sorted(_LOADERS)


def load_document(
    path: str,
    asset_dir: str | None = None,
    *,
    allow_confirmation: bool = False,
    confirm: bool | None = None,
) -> str:
    """按扩展名把文档加载为（富）文本。

    asset_dir：若提供，PDF 中的图片会被抽取保存到该目录。
    """
    text, _report = load_document_with_quality(
        path,
        asset_dir=asset_dir,
        allow_confirmation=allow_confirmation,
        confirm=confirm,
    )
    return text


def load_document_with_quality(
    path: str,
    asset_dir: str | None = None,
    *,
    allow_confirmation: bool = False,
    confirm: bool | None = None,
) -> tuple[str, IngestionQualityReport]:
    """加载并检查质量；严重损坏拒绝，边缘结构须显式确认。"""
    if not os.path.isfile(path):
        raise DocumentLoadError(f"文件不存在：{path}")
    ext = os.path.splitext(path)[1].lower()
    loader = _LOADERS.get(ext)
    if loader is None:
        raise DocumentLoadError(
            f"不支持的文件类型 {ext}；支持：{', '.join(supported_extensions())}"
        )
    text = loader(path, asset_dir)
    if ext == ".pdf":
        text = normalize_extracted_text(text, strip_pdf_noise=True)
    page_count = _pdf_page_count(path) if ext == ".pdf" else None
    report = assess_ingestion_quality(
        text, page_count=page_count, source_type=ext
    )
    if not report.is_acceptable:
        reasons = "；".join(report.fatal_reasons)
        raise DocumentLoadError(
            f"文档摄入质量检查失败（评分 {report.score}/100）：{reasons}"
        )
    allowed = allow_confirmation if confirm is None else bool(confirm)
    if report.confirmation_required and not allowed:
        raise IngestionConfirmationRequired(path, report)
    return text, report


def _pdf_page_count(path: str) -> int | None:
    """尽力读取 PDF 页数，不让可选依赖问题掩盖正文加载结果。"""
    try:
        import fitz  # PyMuPDF  # noqa: WPS433

        with fitz.open(path) as doc:
            return len(doc)
    except Exception:
        try:
            from pypdf import PdfReader  # noqa: WPS433

            return len(PdfReader(path).pages)
        except Exception:
            return None
