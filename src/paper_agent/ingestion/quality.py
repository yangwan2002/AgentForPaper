"""摄入文本质量评估。

评估完全确定性执行，不依赖 LLM；严重损坏会在文本进入工作区前被拒绝，
边缘问题则以可序列化 profile 保留给 CLI、Agent 和评测调用方消费。
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field

from paper_agent.agent_platform.acceptance import detect_mojibake
from paper_agent.ingestion.sections import normalize_extracted_text


_CID = re.compile(r"(?:\(\s*cid\s*:\s*\d+\s*\)|\bcid\s*:\s*\d+)", re.IGNORECASE)
_STRUCTURAL_HEADING = re.compile(
    r"(?m)^\s*(?:#{1,6}\s+\S|\\(?:chapter|section|subsection)\*?\{[^}]+\}|"
    r"\d{1,2}(?:\.\d{1,2}){0,3}[\s、.]+[^\W\d_])"
)


@dataclass(frozen=True)
class IngestionQualityReport:
    """可序列化的摄入质量报告。"""

    score: int
    severity: str
    status: str = "accepted"
    warnings: list[str] = field(default_factory=list)
    fatal_reasons: list[str] = field(default_factory=list)
    metrics: dict[str, float | int | None] = field(default_factory=dict)

    @property
    def is_acceptable(self) -> bool:
        return self.status != "rejected" and not self.fatal_reasons

    @property
    def confirmation_required(self) -> bool:
        return self.status == "confirmation_required"

    def to_profile(self) -> dict:
        return asdict(self)


def assess_ingestion_quality(
    text: str,
    *,
    page_count: int | None = None,
    source_type: str = "",
) -> IngestionQualityReport:
    """评估抽取文本；阈值偏保守，避免把正常英文或短文档误拒绝。"""
    normalized = normalize_extracted_text(
        text, strip_pdf_noise=source_type.lower() == ".pdf"
    )
    total = len(text)
    nonspace = sum(not ch.isspace() for ch in text)
    replacement_count = text.count("\ufffd")
    cid_count = len(_CID.findall(text))
    mojibake_detected, mojibake_evidence = detect_mojibake(text)
    cjk_count = sum("\u3400" <= ch <= "\u9fff" for ch in text)
    letters = sum(ch.isalpha() for ch in text)
    printable_count = sum(ch.isprintable() or ch in "\n\r\t" for ch in text)
    heading_count = len(_STRUCTURAL_HEADING.findall(normalized))
    structural_score = 100
    if nonspace >= 3000:
        if heading_count >= 4:
            structural_score = 100
        elif heading_count >= 2:
            structural_score = 70
        elif heading_count == 1:
            structural_score = 40
        else:
            structural_score = 0

    denominator = max(nonspace, 1)
    metrics: dict[str, float | int | None] = {
        "character_count": total,
        "non_whitespace_count": nonspace,
        "page_count": page_count,
        "section_heading_count": heading_count,
        "structural_score": structural_score,
        "mojibake_detected": mojibake_detected,
        "cid_noise_count": cid_count,
        "cid_noise_ratio": cid_count / denominator,
        "replacement_char_count": replacement_count,
        "replacement_char_ratio": replacement_count / denominator,
        "cjk_ratio": cjk_count / max(letters, 1),
        "printable_ratio": printable_count / max(total, 1),
    }

    warnings: list[str] = []
    fatal: list[str] = []
    penalty = 0

    if not text or not text.strip():
        fatal.append("未抽取到可用文本（文档可能为空或为未 OCR 的扫描件）")
        penalty += 100

    printable_ratio = float(metrics["printable_ratio"])
    replacement_ratio = float(metrics["replacement_char_ratio"])
    cid_ratio = float(metrics["cid_noise_ratio"])

    if printable_ratio < 0.70 and total:
        fatal.append(f"可打印字符比例过低（{printable_ratio:.1%}）")
        penalty += 50
    elif printable_ratio < 0.92 and total:
        warnings.append(f"可打印字符比例偏低（{printable_ratio:.1%}）")
        penalty += 15

    if (replacement_count >= 5 and replacement_ratio >= 0.02) or replacement_count >= 100:
        fatal.append(f"Unicode replacement char 过多（{replacement_count} 个）")
        penalty += 45
    elif replacement_count:
        warnings.append(f"检测到 Unicode replacement char（{replacement_count} 个）")
        penalty += min(15, replacement_count)

    if (cid_count >= 5 and cid_ratio >= 0.05) or cid_count >= 100:
        fatal.append(f"PDF cid 噪声过多（{cid_count} 处）")
        penalty += 45
    elif cid_count:
        warnings.append(f"检测到 PDF cid 噪声（{cid_count} 处）")
        penalty += min(15, cid_count)

    # 乱码识别统一复用验收层 detector，避免摄入与导出验收规则分别漂移。
    # 单个 replacement char 仍按上面的数量阈值区分警告/致命；其余编码错配
    # （如连续 latin-1 高位字符）是强信号，直接拒绝。
    if mojibake_detected and replacement_count == 0:
        fatal.append(f"疑似乱码（mojibake）：{mojibake_evidence}")
        penalty += 45

    if page_count is not None:
        if page_count <= 0:
            fatal.append("文档页数异常（0 页）")
            penalty += 100
        elif page_count > 1 and nonspace / page_count < 40:
            warnings.append(
                f"每页抽取文本过少（{nonspace / page_count:.1f} 字符/页），可能是扫描件"
            )
            penalty += 20

    confirmation_required = False
    # 正文可读但长文缺少可识别章节结构，需要调用方明确确认后才能继续。
    if nonspace >= 3000 and heading_count < 2:
        warnings.append("长文档正文可读，但未检测到足够章节结构")
        confirmation_required = True
        penalty += 10
    if heading_count > 200 or (
        page_count and page_count > 0 and heading_count > max(30, page_count * 8)
    ):
        warnings.append(f"章节结构异常密集（检测到 {heading_count} 个标题）")
        penalty += 10

    # CJK 比例始终记录；混入少量 CJK 本身不是损坏，避免误伤双语论文。
    if source_type.lower() == ".pdf" and 0 < cjk_count < 3 and mojibake_detected:
        warnings.append("CJK 字符比例异常且伴随乱码，可能存在字体编码问题")
        penalty += 5

    score = max(0, 100 - penalty)
    severity = "error" if fatal else ("warning" if warnings else "ok")
    status = (
        "rejected"
        if fatal
        else ("confirmation_required" if confirmation_required else "accepted")
    )
    return IngestionQualityReport(
        score=score,
        severity=severity,
        status=status,
        warnings=warnings,
        fatal_reasons=fatal,
        metrics=metrics,
    )


__all__ = ["IngestionQualityReport", "assess_ingestion_quality"]
