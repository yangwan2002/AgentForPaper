"""DraftAuditor 单元测试（inplace-polish-audit · Task 2）。

覆盖：有/无参考文献、检索可用/不可用、判定器可用/不可用、故障隔离；
断言只读隔离（不写 repo、原稿字节不变）。用 fake verifier + fake agent 确定可测。
"""

from __future__ import annotations

from paper_agent.agent_platform.audit import AuditReport, DraftAuditor
from paper_agent.agents.base import AgentResult
from paper_agent.tools.citation import VerificationResult
from paper_agent.workspace.models import ReferenceEntry

_TEX = r"""\documentclass{article}
\begin{document}
\section{Introduction}
We build on prior work [1] and a fabricated claim [2].
\section{参考文献}
[1] Alice. A Real Study. 2021.
[2] Bob. A Fake Study. 2020.
\end{document}
"""


class _FakeVerifier:
    """按标题关键词判定真伪：含 "Real" → 存在；否则不存在。"""

    def verify_by_metadata(self, ref):
        title = (ref.title or "")
        if "Real" in title:
            return VerificationResult(
                exists=True,
                matched=ReferenceEntry(
                    id="x", title=title, authors=["Alice"], year=2021,
                    source_id="doi:1", source="openalex", verified=True,
                ),
                title_score=0.99,
            )
        return VerificationResult(exists=False, note="疑似不存在")


class _FakeFaithAgent:
    """把 ws 里每个已验证引用标为 unsupported，验证发现能被收集。"""

    def run(self, ctx):
        ws = ctx.workspace
        findings = []
        for sid, draft in ws.section_drafts.items():
            for r in ws.verified_references:
                if f"[{r.id}]" in (draft.content or ""):
                    findings.append({
                        "section_id": sid, "cited_reference_id": r.id,
                        "claim_excerpt": "某论断", "verdict": "unsupported",
                        "severity": "high", "rationale": "摘要未支撑",
                    })

        def _mutate(w):
            w.citation_faithfulness = findings

        return AgentResult(mutations=[_mutate])


def _write(tmp_path, text=_TEX):
    src = tmp_path / "paper.tex"
    src.write_text(text, encoding="utf-8")
    return src


def test_authenticity_distinguishes_real_and_fake(tmp_path):
    src = _write(tmp_path)
    original = src.read_bytes()
    auditor = DraftAuditor(_FakeVerifier(), None, retrieval_available=True)
    report = auditor.audit(str(src))

    assert isinstance(report, AuditReport) and report.ran
    assert report.reference_total == 2
    assert report.reference_real == 1
    assert report.reference_unverifiable == 1
    verdicts = {f.index: f.verdict for f in report.authenticity}
    assert verdicts[1] == "real"
    assert verdicts[2] == "unverifiable"
    # 原稿只读：字节不变。
    assert src.read_bytes() == original


def test_retrieval_unavailable_marks_all(tmp_path):
    src = _write(tmp_path)
    auditor = DraftAuditor(_FakeVerifier(), None, retrieval_available=False)
    report = auditor.audit(str(src))
    assert report.reference_total == 2
    assert report.reference_unverifiable == 2
    assert all(f.verdict == "retrieval_unavailable" for f in report.authenticity)
    # 未做真实性 → 不应有 supported 忠实性（判定器也为 None）。
    assert report.faithfulness == []


def test_faithfulness_collected_when_agent_present(tmp_path):
    src = _write(tmp_path)
    auditor = DraftAuditor(
        _FakeVerifier(), _FakeFaithAgent(), retrieval_available=True
    )
    report = auditor.audit(str(src))
    # 只有 [1]（Real，已验证）会被 fake agent 标记；[2] 未验证不进 verified_references。
    assert any(
        f["verdict"] == "unsupported" and f["cited_reference_id"] == "1"
        for f in report.faithfulness
    )
    assert report.has_findings() is True


def test_no_faithfulness_agent_notes(tmp_path):
    src = _write(tmp_path)
    report = DraftAuditor(_FakeVerifier(), None, retrieval_available=True).audit(str(src))
    assert any("判定器不可用" in n for n in report.notes)


def test_no_references_reports_gracefully(tmp_path):
    src = _write(tmp_path, text="\\section{Intro}\nJust prose, no references.\n")
    report = DraftAuditor(_FakeVerifier(), None, retrieval_available=True).audit(str(src))
    assert report.ran
    assert report.reference_total == 0
    assert any("未发现可解析的参考文献" in n for n in report.notes)


def test_missing_file_does_not_raise(tmp_path):
    report = DraftAuditor(_FakeVerifier(), None, retrieval_available=True).audit(
        str(tmp_path / "nope.tex")
    )
    assert report.ran
    assert report.notes  # 记录了未能审计的原因


def test_verifier_exception_isolated(tmp_path):
    class _BoomVerifier:
        def verify_by_metadata(self, ref):
            raise RuntimeError("boom")

    src = _write(tmp_path)
    report = DraftAuditor(_BoomVerifier(), None, retrieval_available=True).audit(str(src))
    # 单条核验抛异常按不可核验处理，不抛出、不连累。
    assert report.ran
    assert report.reference_unverifiable == 2
