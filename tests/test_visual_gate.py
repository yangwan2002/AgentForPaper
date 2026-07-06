"""visual-layout-acceptance 波4 测试：验收闸编排 + 有界重编辑循环。

覆盖 Property 6（有界终止）、7（建议性不阻断）、8（诚实不谎报）、9（优雅降级）。
用 fake 渲染后端 + fake VisualJudge（不依赖真实 Word/PyMuPDF/vision）。
"""

from __future__ import annotations

from paper_agent.agent_platform.visual.gate import VisualAcceptanceGate
from paper_agent.agent_platform.visual.judge import VisualVerdict


class _FakeBackend:
    name = "fake"
    fidelity_note = ""

    def available(self):
        return True

    def render(self, docx_path, out_pdf):
        with open(out_pdf, "wb") as fh:
            fh.write(b"pdf")
        return True


class _LibreLike(_FakeBackend):
    name = "libreoffice"
    fidelity_note = "LibreOffice 渲染与 Word 可能有差异。"


class _FakeJudge:
    """按预置裁定序列返回；记录被调次数。"""

    def __init__(self, verdicts):
        self._verdicts = list(verdicts)
        self.calls = 0

    def judge(self, pages, requirement):
        self.calls += 1
        idx = min(self.calls - 1, len(self._verdicts) - 1)
        return self._verdicts[idx]


def _gate(judge, backend=None):
    # vlm 非 None 以通过"未配置"检查；judge 注入 fake。
    g = VisualAcceptanceGate(vlm=object(), judge=judge, backend=backend or _FakeBackend())
    return g


def _rasterize_ok(monkeypatch, pages=("p1.png",)):
    """让 rasterize_pdf 返回稳定的假页（避免依赖 PyMuPDF）。"""
    import paper_agent.agent_platform.visual.gate as gate_mod

    def fake_raster(pdf, out_dir, *, dpi=150):
        import os
        outs = []
        for i, _ in enumerate(pages):
            p = os.path.join(out_dir, f"pg{i}.png")
            with open(p, "wb") as fh:
                fh.write(b"IMG")
            outs.append(p)
        return outs

    monkeypatch.setattr(gate_mod, "rasterize_pdf", fake_raster)


def test_satisfied_first_round(tmp_path, monkeypatch):
    _rasterize_ok(monkeypatch)
    docx = tmp_path / "a.docx"; docx.write_bytes(b"x")
    judge = _FakeJudge([VisualVerdict(satisfied=True)])
    out = _gate(judge).evaluate(str(docx), "图跨双栏", heal_fn=lambda d: None, max_rounds=2)
    assert out.ran and out.satisfied
    assert judge.calls == 1
    assert out.rounds == 0


def test_heal_then_satisfied(tmp_path, monkeypatch):
    _rasterize_ok(monkeypatch)
    docx = tmp_path / "a.docx"; docx.write_bytes(b"x")
    heals = []
    judge = _FakeJudge([
        VisualVerdict(satisfied=False, defects=["图上方空白"]),
        VisualVerdict(satisfied=True),
    ])
    out = _gate(judge).evaluate(
        str(docx), "图跨双栏", heal_fn=lambda d: heals.append(d), max_rounds=2
    )
    assert out.satisfied
    assert out.rounds == 1
    assert heals == [["图上方空白"]]          # 缺陷被反馈给编辑


def test_bounded_heal_calls_never_exceed_max_rounds(tmp_path, monkeypatch):
    _rasterize_ok(monkeypatch)
    docx = tmp_path / "a.docx"; docx.write_bytes(b"x")
    heals = []
    # 永远不满足。
    judge = _FakeJudge([VisualVerdict(satisfied=False, defects=["坏"])])
    out = _gate(judge).evaluate(
        str(docx), "诉求", heal_fn=lambda d: heals.append(d), max_rounds=2
    )
    assert out.satisfied is False           # Property 8：不谎报
    assert out.defects == ["坏"]             # Property 7：附缺陷、仍交付（ran=True）
    assert out.ran is True
    assert len(heals) <= 2                   # Property 6：重改次数 ≤ max_rounds
    assert out.rounds == 2


def test_max_rounds_zero_no_heal(tmp_path, monkeypatch):
    _rasterize_ok(monkeypatch)
    docx = tmp_path / "a.docx"; docx.write_bytes(b"x")
    heals = []
    judge = _FakeJudge([VisualVerdict(satisfied=False, defects=["x"])])
    out = _gate(judge).evaluate(
        str(docx), "诉求", heal_fn=lambda d: heals.append(d), max_rounds=0
    )
    assert heals == []                       # 0 轮 → 不重改
    assert out.satisfied is False


def test_unparsed_verdict_does_not_drive_heal(tmp_path, monkeypatch):
    _rasterize_ok(monkeypatch)
    docx = tmp_path / "a.docx"; docx.write_bytes(b"x")
    heals = []
    judge = _FakeJudge([VisualVerdict(satisfied=False, parsed=False)])
    out = _gate(judge).evaluate(
        str(docx), "诉求", heal_fn=lambda d: heals.append(d), max_rounds=3
    )
    assert heals == []                       # 不可信 → 不重改、不卡
    assert out.ran is True and out.satisfied is False


def test_skip_when_no_vlm(tmp_path):
    docx = tmp_path / "a.docx"; docx.write_bytes(b"x")
    g = VisualAcceptanceGate(vlm=None)
    out = g.evaluate(str(docx), "诉求", heal_fn=lambda d: None, max_rounds=1)
    assert out.ran is False
    assert "未配置多模态" in out.skip_reason


def test_skip_when_no_backend(tmp_path, monkeypatch):
    import paper_agent.agent_platform.visual.gate as gate_mod
    monkeypatch.setattr(gate_mod, "select_render_backend", lambda p=None: None)
    docx = tmp_path / "a.docx"; docx.write_bytes(b"x")
    g = VisualAcceptanceGate(vlm=object(), judge=_FakeJudge([VisualVerdict(satisfied=True)]))
    out = g.evaluate(str(docx), "诉求", heal_fn=lambda d: None, max_rounds=1)
    assert out.ran is False
    assert "渲染后端" in out.skip_reason


def test_skip_when_render_fails(tmp_path, monkeypatch):
    _rasterize_ok(monkeypatch)
    docx = tmp_path / "a.docx"; docx.write_bytes(b"x")

    class _BadBackend(_FakeBackend):
        def render(self, docx_path, out_pdf):
            return False

    out = _gate(_FakeJudge([VisualVerdict(satisfied=True)]), backend=_BadBackend()).evaluate(
        str(docx), "诉求", heal_fn=lambda d: None, max_rounds=1
    )
    assert out.ran is False
    assert "渲染" in out.skip_reason


def test_libreoffice_fidelity_note_carried(tmp_path, monkeypatch):
    _rasterize_ok(monkeypatch)
    docx = tmp_path / "a.docx"; docx.write_bytes(b"x")
    out = _gate(_FakeJudge([VisualVerdict(satisfied=True)]), backend=_LibreLike()).evaluate(
        str(docx), "诉求", heal_fn=lambda d: None, max_rounds=1
    )
    assert out.satisfied
    assert out.fidelity_note                 # Property 11：LibreOffice 附保真告警
    assert "LibreOffice" in out.message()
