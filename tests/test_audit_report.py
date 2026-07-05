"""AuditReport / render 单元测试（inplace-polish-audit · Task 1）。

覆盖：有问题/无问题渲染、摘录脱敏截断、空/未运行报告、has_findings 语义。
"""

from __future__ import annotations

from paper_agent.agent_platform.audit import AuditReport, ReferenceAuthenticityFinding


def test_not_ran_renders_empty():
    assert AuditReport(ran=False).render() == ""


def test_clean_report_states_no_problems():
    report = AuditReport(
        ran=True,
        reference_total=3,
        reference_real=3,
        reference_unverifiable=0,
        authenticity=[
            ReferenceAuthenticityFinding(1, "A Study", "real"),
            ReferenceAuthenticityFinding(2, "B Study", "real"),
        ],
        faithfulness=[
            {"verdict": "supported", "cited_reference_id": "1", "section_id": "s1",
             "claim_excerpt": "x", "rationale": ""},
        ],
    )
    assert report.has_findings() is False
    text = report.render()
    assert "均可核验" in text
    assert "未发现明显不支撑" in text


def test_reports_unverifiable_reference():
    report = AuditReport(
        ran=True,
        reference_total=2,
        reference_real=1,
        reference_unverifiable=1,
        authenticity=[
            ReferenceAuthenticityFinding(1, "Real Paper", "real"),
            ReferenceAuthenticityFinding(2, "Fake Paper", "unverifiable"),
        ],
    )
    assert report.has_findings() is True
    text = report.render()
    assert "未核验" in text
    assert "Fake Paper" in text
    assert "[2]" in text


def test_reports_unsupported_faithfulness():
    report = AuditReport(
        ran=True,
        reference_total=1,
        reference_real=1,
        faithfulness=[
            {"verdict": "unsupported", "cited_reference_id": "3", "section_id": "intro",
             "claim_excerpt": "本文首次提出该方法", "rationale": "被引文献未涉及该方法"},
        ],
    )
    assert report.has_findings() is True
    text = report.render()
    assert "不支撑" in text
    assert "[3]" in text
    assert "intro" in text


def test_excerpt_is_truncated():
    long_title = "标题" * 500
    report = AuditReport(
        ran=True, reference_total=1, reference_real=0, reference_unverifiable=1,
        authenticity=[ReferenceAuthenticityFinding(1, long_title, "unverifiable")],
        excerpt_max=50,
    )
    text = report.render()
    # 渲染中不应出现完整的超长标题（被截断到 excerpt_max=50 字符 = 25 个「标题」）。
    assert long_title not in text
    assert "标题" * 25 in text


def test_no_references_parsed_message():
    report = AuditReport(ran=True, reference_total=0)
    text = report.render()
    assert "未发现可解析的参考文献表" in text


def test_notes_rendered():
    report = AuditReport(ran=True, notes=["检索不可用，未做真实性核验"])
    text = report.render()
    assert "检索不可用" in text
