"""inplace-polish-audit 属性测试（Task 5）。

覆盖：原稿只读（Property 3）、故障隔离绝不抛出（Property 5）、检索不可用即全不可核验
且绝不假判 supported（Property 4/6）、报告摘录有界（Property 8）、向后兼容（Property 7）。
"""

from __future__ import annotations

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from paper_agent.agent_platform.audit import AuditReport, DraftAuditor
from paper_agent.agents.base import AgentResult
from paper_agent.tools.citation import VerificationResult

_ALLOW_TMP = [HealthCheck.function_scoped_fixture]

_TEX_TMPL = (
    "\\documentclass{{article}}\n\\begin{{document}}\n\\section{{Intro}}\n"
    "{body} We cite [1] and [2].\n\\section{{参考文献}}\n"
    "[1] Alice. {t1}. 2021.\n[2] Bob. {t2}. 2020.\n\\end{{document}}\n"
)


class _FakeVerifier:
    def __init__(self, exists: bool):
        self._exists = exists

    def verify_by_metadata(self, ref):
        if self._exists:
            from paper_agent.workspace.models import ReferenceEntry

            return VerificationResult(
                exists=True,
                matched=ReferenceEntry(
                    id="x", title=ref.title, authors=["A"], year=2021,
                    source_id="doi", source="openalex", verified=True,
                ),
                title_score=0.99,
            )
        return VerificationResult(exists=False, note="no")


class _AlwaysSupportedAgent:
    """对任意已验证引用都判 supported——用于验证"检索不可用时不会产生 supported"。"""

    def run(self, ctx):
        ws = ctx.workspace
        findings = [
            {"section_id": "s", "cited_reference_id": r.id, "claim_excerpt": "x",
             "verdict": "supported", "severity": "none", "rationale": ""}
            for r in ws.verified_references
        ]
        return AgentResult(mutations=[lambda w: setattr(w, "citation_faithfulness", findings)])


class _BoomVerifier:
    def verify_by_metadata(self, ref):
        raise RuntimeError("boom")


def _write(tmp_path, body="Prose."):
    src = tmp_path / "p.tex"
    src.write_text(
        _TEX_TMPL.format(body=body, t1="A Study", t2="B Study"), encoding="utf-8"
    )
    return src


# Property 3: 原稿只读——审计后原稿字节不变。
@settings(max_examples=25, deadline=None, suppress_health_check=_ALLOW_TMP)
@given(body=st.text(alphabet="abc DEF.,", min_size=0, max_size=40))
def test_prop3_source_bytes_unchanged(body, tmp_path):
    src = _write(tmp_path, body=body or "x")
    original = src.read_bytes()
    DraftAuditor(_FakeVerifier(True), None, retrieval_available=True).audit(str(src))
    assert src.read_bytes() == original


# Property 5: 故障隔离——任一子步骤抛异常，audit 不抛出、仍返回报告。
@settings(max_examples=15, deadline=None, suppress_health_check=_ALLOW_TMP)
@given(available=st.booleans())
def test_prop5_never_raises(available, tmp_path):
    src = _write(tmp_path)
    report = DraftAuditor(_BoomVerifier(), None, retrieval_available=available).audit(
        str(src)
    )
    assert isinstance(report, AuditReport) and report.ran


# Property 4/6: 检索不可用 → 真实性全 retrieval_unavailable，且忠实性无 supported。
def test_prop6_retrieval_unavailable_no_supported(tmp_path):
    src = _write(tmp_path)
    report = DraftAuditor(
        _FakeVerifier(True), _AlwaysSupportedAgent(), retrieval_available=False
    ).audit(str(src))
    assert report.reference_total == 2
    assert all(f.verdict == "retrieval_unavailable" for f in report.authenticity)
    # 检索不可用 → 无已验证引用 → 判定器即便"总判 supported"也拿不到条目。
    assert not any(f.get("verdict") == "supported" for f in report.faithfulness)


# Property 4: 未通过真实性核验的引用不会产生 supported。
def test_prop4_unverified_refs_never_supported(tmp_path):
    src = _write(tmp_path)
    report = DraftAuditor(
        _FakeVerifier(False), _AlwaysSupportedAgent(), retrieval_available=True
    ).audit(str(src))
    assert report.reference_real == 0
    assert not any(f.get("verdict") == "supported" for f in report.faithfulness)


# Property 8: 报告摘录有界。
@settings(max_examples=20)
@given(
    n_unver=st.integers(min_value=0, max_value=5),
    title_len=st.integers(min_value=0, max_value=600),
)
def test_prop8_render_excerpt_bounded(n_unver, title_len):
    from paper_agent.agent_platform.audit import ReferenceAuthenticityFinding

    limit = 40
    auth = [
        ReferenceAuthenticityFinding(i + 1, "标" * title_len, "unverifiable", "备" * title_len)
        for i in range(n_unver)
    ]
    report = AuditReport(
        ran=True, reference_total=n_unver, reference_unverifiable=n_unver,
        authenticity=auth, excerpt_max=limit,
    )
    text = report.render()
    # 任一超长标题/备注都不应以完整形态出现（被截断到 limit）。
    if title_len > limit:
        assert "标" * title_len not in text
        assert "备" * title_len not in text
