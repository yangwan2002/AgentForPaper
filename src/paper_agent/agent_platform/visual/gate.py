"""视觉验收闸 + 有界重编辑循环（visual-layout-acceptance · Task 7）。

编排：选渲染后端 → 渲染前/后两版并挑变化页 → 多模态判断 → 不满足则有界重改 →
达上限诚实收尾。全程**建议性**（视觉误判不阻断交付）、**处处优雅降级**（依赖缺失 /
渲染或视觉失败即 skip、回退既有行为、不抛）、**只读不碰正确性核心**（重改经 heal_fn
走既有写路径）。
"""

from __future__ import annotations

import os
import tempfile
from dataclasses import dataclass, field
from typing import Callable

from paper_agent.agent_platform.visual.judge import VisualJudge, VisualVerdict
from paper_agent.agent_platform.visual.page_select import select_pages_to_judge
from paper_agent.export.doc_render import RenderBackend, rasterize_pdf, select_render_backend


@dataclass
class VisualAcceptanceOutcome:
    """一次视觉验收的结构化结论。"""

    ran: bool                       # 是否实际执行（False=被 skip）
    satisfied: bool = False
    defects: list[str] = field(default_factory=list)
    rounds: int = 0                 # 实际重编辑轮数
    fidelity_note: str = ""         # LibreOffice 后端时非空
    skip_reason: str = ""           # ran=False 时说明
    backend: str = ""
    sampled: bool = False           # 是否因超页数上限而仅采样

    def message(self) -> str:
        """人可读结论（并入回复；诚实：未满足不谎报）。"""
        if not self.ran:
            return ""  # skip 默认安静（调用方可选择是否提示）
        lines: list[str] = []
        if self.satisfied:
            lines.append("✓ 视觉版面校验通过（建议性判断，区别于确定性结构/文本验收）。")
        else:
            lines.append("⚠ 视觉版面校验未达成（已如实上报，未谎报成功）：")
            for d in self.defects:
                lines.append(f"  - {d}")
        if self.sampled:
            lines.append("  （注：页面较多，仅采样部分页面判断。）")
        if self.fidelity_note:
            lines.append("  " + self.fidelity_note)
        return "\n".join(lines)


class VisualAcceptanceGate:
    """视觉验收闸：渲染→看图→有界重改→诚实上报。"""

    def __init__(
        self,
        vlm,
        *,
        judge: VisualJudge | None = None,
        backend: RenderBackend | None = None,
        soffice_path: str | None = None,
    ) -> None:
        self._vlm = vlm
        self._judge = judge or (VisualJudge(vlm) if vlm is not None else None)
        self._backend = backend            # 可注入（测试）；否则运行时 select
        self._soffice_path = soffice_path

    def _render_pages(self, docx_path: str, work_dir: str, backend: RenderBackend, dpi: int) -> list[str]:
        """渲染 docx → 逐页 png；任一步失败返回 []（不抛）。"""
        pdf = os.path.join(work_dir, os.path.splitext(os.path.basename(docx_path))[0] + ".pdf")
        if not backend.render(docx_path, pdf):
            return []
        return rasterize_pdf(pdf, work_dir, dpi=dpi)

    def evaluate(
        self,
        docx_path: str,
        layout_requirement: str,
        *,
        baseline_docx: str | None = None,
        heal_fn: Callable[[list[str]], None] | None = None,
        max_rounds: int = 1,
        dpi: int = 150,
        max_pages: int = 6,
    ) -> VisualAcceptanceOutcome:
        """执行视觉验收（含有界重改）。任何依赖缺失/失败 → ran=False 优雅降级。"""
        if self._vlm is None or self._judge is None:
            return VisualAcceptanceOutcome(ran=False, skip_reason="未配置多模态模型")
        backend = self._backend or select_render_backend(self._soffice_path)
        if backend is None:
            return VisualAcceptanceOutcome(ran=False, skip_reason="无可用渲染后端")

        current_baseline = baseline_docx
        rounds = 0
        last_verdict: VisualVerdict | None = None
        try:
            for attempt in range(max(0, int(max_rounds)) + 1):
                with tempfile.TemporaryDirectory(prefix="_vla_") as work:
                    after_dir = os.path.join(work, "after")
                    os.makedirs(after_dir, exist_ok=True)
                    after_imgs = self._render_pages(docx_path, after_dir, backend, dpi)
                    if not after_imgs:
                        return VisualAcceptanceOutcome(
                            ran=False, skip_reason="渲染/栅格化失败", backend=backend.name
                        )
                    before_imgs: list[str] | None = None
                    if current_baseline and os.path.isfile(current_baseline):
                        before_dir = os.path.join(work, "before")
                        os.makedirs(before_dir, exist_ok=True)
                        before_imgs = self._render_pages(current_baseline, before_dir, backend, dpi) or None

                    pages, sampled = select_pages_to_judge(
                        before_imgs, after_imgs, max_pages=max_pages
                    )
                    verdict = self._judge.judge(pages, layout_requirement)
                    last_verdict = verdict

                    # 解析失败 = 不可信：不驱动重改、不卡产物（建议性）。
                    if not verdict.parsed:
                        return VisualAcceptanceOutcome(
                            ran=True, satisfied=False, defects=[], rounds=rounds,
                            fidelity_note=backend.fidelity_note, backend=backend.name,
                            sampled=sampled, skip_reason="视觉判断不可信",
                        )
                    if verdict.satisfied:
                        return VisualAcceptanceOutcome(
                            ran=True, satisfied=True, rounds=rounds,
                            fidelity_note=backend.fidelity_note, backend=backend.name,
                            sampled=sampled,
                        )
                    # 未满足且还能重改 → 反馈缺陷让编辑智能体改（走既有写路径）。
                    if attempt < max_rounds and heal_fn is not None:
                        current_baseline = docx_path  # 下一轮以本轮产物为 diff 基线
                        heal_fn(verdict.defects)
                        rounds += 1
                        continue
                    # 达上限仍未满足 → 诚实收尾（不阻断交付）。
                    return VisualAcceptanceOutcome(
                        ran=True, satisfied=False, defects=verdict.defects, rounds=rounds,
                        fidelity_note=backend.fidelity_note, backend=backend.name,
                        sampled=sampled,
                    )
        except Exception as exc:  # noqa: BLE001 - 闸门整体故障隔离，绝不拖垮主流程
            return VisualAcceptanceOutcome(
                ran=False, skip_reason=f"视觉验收异常：{type(exc).__name__}",
                backend=getattr(backend, "name", ""),
            )
        # 理论不可达（循环内必 return）；兜底诚实。
        return VisualAcceptanceOutcome(
            ran=True, satisfied=bool(last_verdict and last_verdict.satisfied),
            defects=(last_verdict.defects if last_verdict else []), rounds=rounds,
            backend=backend.name,
        )


__all__ = ["VisualAcceptanceOutcome", "VisualAcceptanceGate"]
