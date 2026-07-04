"""GuardrailGate 单元测试（任务 2）。

用轻量 fake 注入抽象护栏（质量闸 / 核验器 / 忠实性筛查），验证：
- 内容改动按目标章节归因高严重度问题，未通过则拒绝、通过则接受；
- 引用增补只接受可核验文献，产差额说明，绝不落盘不可核验文献；
- accepted / rejected 划分完备且不重叠；
- dry-run 不污染真实工作区。
"""

from __future__ import annotations

from dataclasses import dataclass

from paper_agent.agent_platform.guardrail_gate import GuardrailGate
from paper_agent.agent_platform.models import (
    CHANGE_CITATION,
    CHANGE_CONTENT,
    ProposedChange,
)
from paper_agent.workspace.models import (
    InputMode,
    OutlineNode,
    PaperWorkspace,
    ReferenceEntry,
    SectionDraft,
)


# --- fakes -------------------------------------------------------------------

@dataclass
class _Report:
    issues: list


class _FakeQuality:
    """按注入的 issues 列表返回；便于精确控制归因场景。"""

    def __init__(self, issues=None):
        self._issues = issues or []

    def check(self, ws):
        return _Report(issues=list(self._issues))


class _FakeVerifier:
    """按 source_id 白名单判定可核验。"""

    def __init__(self, verifiable_ids):
        self._ok = set(verifiable_ids)

    def verify(self, entry):
        return entry.source_id in self._ok


class _FakeFaithfulness:
    def __init__(self, reasons_by_section):
        self._map = reasons_by_section

    def unsupported_reasons(self, ws, section_id):
        return list(self._map.get(section_id, []))


# --- 工作区与意图构造 --------------------------------------------------------

def _ws():
    ws = PaperWorkspace(workspace_id="w1", input_mode=InputMode.DRAFT_REVISION)
    ws.outline = [OutlineNode(section_id="intro", title="引言", order=0)]
    ws.section_drafts = {"intro": SectionDraft(section_id="intro", title="引言", content="原文")}
    return ws


def _set_content_mutation(section_id, new_text):
    def _mut(ws):
        ws.section_drafts[section_id].content = new_text
    return _mut


# --- 内容改动 ----------------------------------------------------------------

def test_content_change_accepted_when_no_blocking_issue():
    gate = GuardrailGate(quality_gate=_FakeQuality(issues=[]))
    change = ProposedChange(
        mutation=_set_content_mutation("intro", "更好的引言"),
        kind=CHANGE_CONTENT,
        section_id="intro",
    )
    outcome = gate.screen(_ws(), [change])
    assert outcome.passed is True
    assert len(outcome.accepted_mutations) == 1
    assert outcome.rejected == []


def test_content_change_rejected_on_high_severity_in_target_section():
    quality = _FakeQuality(issues=[
        {"type": "placeholder", "severity": "high", "section_id": "intro", "message": "含占位 TODO"},
    ])
    gate = GuardrailGate(quality_gate=quality)
    change = ProposedChange(
        mutation=_set_content_mutation("intro", "TODO"),
        kind=CHANGE_CONTENT,
        section_id="intro",
    )
    outcome = gate.screen(_ws(), [change])
    assert outcome.passed is False
    assert outcome.accepted_mutations == []
    assert len(outcome.rejected) == 1
    assert "占位" in outcome.rejected[0].reason


def test_content_change_ignores_issue_in_other_section():
    quality = _FakeQuality(issues=[
        {"severity": "high", "section_id": "other", "message": "别的章节的问题"},
    ])
    gate = GuardrailGate(quality_gate=quality)
    change = ProposedChange(
        mutation=_set_content_mutation("intro", "x"),
        kind=CHANGE_CONTENT,
        section_id="intro",
    )
    outcome = gate.screen(_ws(), [change])
    assert outcome.passed is True


def test_completeness_issue_is_advisory_not_blocking():
    # 完整性/风格类高严重度问题（如缺体裁必备元素）不应拦截润色，只作建议。
    quality = _FakeQuality(issues=[
        {"type": "missing_required_element", "severity": "high",
         "section_id": "intro", "message": "缺少体裁必备元素「贡献」"},
    ])
    gate = GuardrailGate(quality_gate=quality)
    change = ProposedChange(
        mutation=_set_content_mutation("intro", "润色后的引言"),
        kind=CHANGE_CONTENT,
        section_id="intro",
    )
    outcome = gate.screen(_ws(), [change])
    assert outcome.passed is True            # 未被拦截
    assert len(outcome.accepted_mutations) == 1
    assert any("贡献" in n for n in outcome.notes)  # 但作为建议带出


def test_content_change_medium_severity_not_blocking():
    quality = _FakeQuality(issues=[
        {"severity": "medium", "section_id": "intro", "message": "略短"},
    ])
    gate = GuardrailGate(quality_gate=quality)
    change = ProposedChange(
        mutation=_set_content_mutation("intro", "x"),
        kind=CHANGE_CONTENT,
        section_id="intro",
    )
    assert gate.screen(_ws(), [change]).passed is True


def test_faithfulness_screener_blocks_unsupported_section():
    gate = GuardrailGate(
        quality_gate=_FakeQuality(issues=[]),
        faithfulness_screener=_FakeFaithfulness({"intro": ["声明[X]无正文支撑"]}),
    )
    change = ProposedChange(
        mutation=_set_content_mutation("intro", "x"),
        kind=CHANGE_CONTENT,
        section_id="intro",
    )
    outcome = gate.screen(_ws(), [change])
    assert outcome.passed is False
    assert "无正文支撑" in outcome.rejected[0].reason


def test_dry_run_does_not_mutate_real_workspace():
    gate = GuardrailGate(quality_gate=_FakeQuality(issues=[]))
    ws = _ws()
    change = ProposedChange(
        mutation=_set_content_mutation("intro", "被改的正文"),
        kind=CHANGE_CONTENT,
        section_id="intro",
    )
    gate.screen(ws, [change])
    # screen 只在副本上 dry-run，真实工作区不变。
    assert ws.section_drafts["intro"].content == "原文"


# --- 引用增补 ----------------------------------------------------------------

def _ref(rid, source_id):
    return ReferenceEntry(id=rid, title=f"T{rid}", authors=["A"], year=2024, source_id=source_id)


def test_citation_only_verifiable_refs_are_accepted_and_land():
    verifier = _FakeVerifier(verifiable_ids={"doi-1"})
    gate = GuardrailGate(citation_verifier=verifier)
    change = ProposedChange(
        mutation=lambda ws: None,  # 引用通道忽略工具原意图
        kind=CHANGE_CITATION,
        references=[_ref("r1", "doi-1"), _ref("r2", "doi-missing")],
    )
    ws = _ws()
    outcome = gate.screen(ws, [change])
    assert len(outcome.accepted_mutations) == 1
    # 应用接受的意图后，只有可核验的 r1 落盘，且被标记 verified。
    outcome.accepted_mutations[0](ws)
    ids = {r.id for r in ws.verified_references}
    assert ids == {"r1"}
    assert all(r.verified for r in ws.verified_references)
    # 差额说明存在。
    assert any("差额" in n for n in outcome.notes)


def test_citation_no_verifier_is_fail_closed():
    gate = GuardrailGate()  # 无核验器
    change = ProposedChange(
        mutation=lambda ws: None,
        kind=CHANGE_CITATION,
        references=[_ref("r1", "doi-1")],
    )
    outcome = gate.screen(_ws(), [change])
    # 无核验器 → 一律不可核验 → 无接受意图、有差额说明。
    assert outcome.accepted_mutations == []
    assert any("差额" in n for n in outcome.notes)


def test_citation_all_verifiable_no_shortfall_note():
    gate = GuardrailGate(citation_verifier=_FakeVerifier({"doi-1", "doi-2"}))
    change = ProposedChange(
        mutation=lambda ws: None,
        kind=CHANGE_CITATION,
        references=[_ref("r1", "doi-1"), _ref("r2", "doi-2")],
    )
    outcome = gate.screen(_ws(), [change])
    assert outcome.notes == []
    assert len(outcome.accepted_mutations) == 1


def test_mixed_batch_partition_complete_and_disjoint():
    quality = _FakeQuality(issues=[
        {"type": "placeholder", "severity": "high", "section_id": "intro", "message": "坏"},
    ])
    gate = GuardrailGate(quality_gate=quality, citation_verifier=_FakeVerifier({"doi-1"}))
    content_bad = ProposedChange(
        mutation=_set_content_mutation("intro", "TODO"),
        kind=CHANGE_CONTENT, section_id="intro",
    )
    citation = ProposedChange(
        mutation=lambda ws: None, kind=CHANGE_CITATION,
        references=[_ref("r1", "doi-1")],
    )
    outcome = gate.screen(_ws(), [content_bad, citation])
    # 内容被拒；引用被接受。二者不重叠、完备。
    assert len(outcome.rejected) == 1
    assert len(outcome.accepted_mutations) == 1
    assert outcome.passed is False  # 有 rejected → 整批未全通过
