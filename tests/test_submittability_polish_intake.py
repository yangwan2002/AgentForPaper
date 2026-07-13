"""投递质量增强测试：结构化访谈录入、语言润色、原创性自检、可投递性判定。

覆盖新增的四块能力（均遵守既有契约：纯数据/单一写入路径/Mock 下 no-op）。
"""

from __future__ import annotations

import pytest

from paper_agent.agents.base import AgentContext
from paper_agent.agents.language_polish_agent import LanguagePolishAgent
from paper_agent.elicitation import ScriptedElicitor
from paper_agent.ingestion.interactive_intake import (
    build_artifact_from_description,
    run_intake,
)
from paper_agent.providers.llm.base import LLMResponse
from paper_agent.tools.originality_check import check_originality, overlap_ratio
from paper_agent.workspace.models import (
    InputMode,
    OutlineNode,
    PaperWorkspace,
    ReferenceEntry,
    SectionDraft,
)
from paper_agent.workspace.submittability import assess_submittability


# --- 测试用 fake LLM ---


class _ScriptedLLM:
    """按注入的映射「原文 → 润色文」返回；未命中则回显原文。"""

    def __init__(self, mapping: dict[str, str]) -> None:
        self._mapping = mapping

    def complete(self, messages, **opts) -> LLMResponse:
        # 章节原文在最后一条 user 消息里。取整段做包含匹配。
        user_text = messages[-1].content
        for src, dst in self._mapping.items():
            if src in user_text:
                return LLMResponse(content=dst)
        return LLMResponse(content="")


def _ws(sections: dict[str, str], mode=InputMode.GENERATION) -> PaperWorkspace:
    ws = PaperWorkspace(workspace_id="wstest", input_mode=mode)
    for i, (sid, content) in enumerate(sections.items()):
        ws.outline.append(OutlineNode(section_id=sid, title=sid.title(), order=i))
        ws.section_drafts[sid] = SectionDraft(section_id=sid, title=sid.title(), content=content)
    return ws


# --- 结构化访谈录入 ---


def test_build_artifact_from_description_basic():
    art = build_artifact_from_description(
        field="空地协同 SLAM",
        problem="大视角与大尺度差图像匹配",
        method="基于跨视角特征对齐的分层匹配网络",
        contributions=["提出分层匹配", ""],
        novelty_claims=["首次在跨视角上验证"],
    )
    assert not art.is_empty()
    assert "空地协同 SLAM" in art.research_question
    assert "大视角" in art.research_question
    assert art.method.overview.startswith("基于跨视角")
    # 空贡献被过滤。
    assert len(art.contributions) == 1
    assert art.experiments == []


@pytest.mark.parametrize(
    "field,problem,method",
    [("", "p", "m"), ("f", "", "m"), ("f", "p", "  ")],
)
def test_build_artifact_requires_all_three(field, problem, method):
    with pytest.raises(ValueError):
        build_artifact_from_description(field=field, problem=problem, method=method)


def test_run_intake_collects_and_builds():
    answers = {
        "field": "空地协同 SLAM",
        "problem": "大尺度差图像匹配",
        "method": "分层匹配网络",
        "contributions": "贡献一；贡献二",
        "novelty": "",
    }
    art = run_intake(ScriptedElicitor(answers))
    assert art is not None
    assert "空地协同 SLAM" in art.research_question
    assert art.contributions[0].summary == "贡献一"
    assert len(art.contributions) == 2


def test_run_intake_missing_required_returns_none():
    # 问题（problem）留空 → 必填不全 → 返回 None。
    answers = {"field": "空地协同 SLAM", "problem": "", "method": "方法"}
    art = run_intake(ScriptedElicitor(answers))
    assert art is None


# --- 原创性自检 ---


def test_overlap_ratio_identical_text_high():
    text = "这 是 一 段 完全 重复 的 学术 文本 用于 测试 重合 度 检测 功能"
    assert overlap_ratio(text, [text], n=3) == pytest.approx(1.0)


def test_overlap_ratio_no_reference_zero():
    assert overlap_ratio("some original text here", [], n=8) == 0.0


def test_check_originality_flags_high_overlap():
    shared = (
        "cross view feature alignment hierarchical matching network for aerial "
        "ground collaborative slam under large viewpoint and scale differences "
        "using deep descriptors and geometric verification pipeline stages "
        "with robust outlier rejection and multi scale pyramid representation "
        "for accurate correspondence estimation across heterogeneous sensor views "
        "enabling reliable localization and mapping in complex outdoor environments"
    )
    ws = _ws({"related": shared})
    ws.verified_references.append(
        ReferenceEntry(
            id="r1", title="prior", authors=["A"], year=2020,
            source_id="x", source="arxiv", verified=True, abstract=shared,
        )
    )
    findings = check_originality(ws, n=5, threshold=0.15)
    assert findings
    assert findings[0]["type"] == "high_text_overlap"
    assert findings[0]["section_id"] == "related"


def test_check_originality_no_refs_empty():
    ws = _ws({"intro": "some sufficiently long original content " * 5})
    assert check_originality(ws) == []


# --- 可投递性判定 ---


def test_submittability_generation_without_artifact_blocked():
    ws = _ws({"intro": "内容"}, mode=InputMode.GENERATION)
    v = assess_submittability(ws, terminated_reason="quality_met")
    assert v.submittable is False
    assert any("LLM 推断版" in b for b in v.blockers)


def test_submittability_generation_with_artifact_and_quality_met_ok():
    ws = _ws({"intro": "内容"}, mode=InputMode.GENERATION)
    ws.artifact = build_artifact_from_description(
        field="f", problem="p", method="m"
    )
    v = assess_submittability(ws, terminated_reason="quality_met")
    assert v.submittable is True


def test_submittability_quality_not_met_blocked():
    ws = _ws({"intro": "内容"}, mode=InputMode.DRAFT_REVISION)
    v = assess_submittability(ws, terminated_reason="iteration_limit")
    assert v.submittable is False
    assert any("质量闸未通过" in b for b in v.blockers)


def test_submittability_format_fail_note_blocked():
    ws = _ws({"intro": "内容"}, mode=InputMode.DRAFT_REVISION)
    v = assess_submittability(
        ws,
        terminated_reason="quality_met",
        export_notes=["格式未通过：已达修复上限；最后错误：xxx"],
    )
    assert v.submittable is False


def test_submittability_originality_is_caution_not_blocker():
    ws = _ws({"intro": "内容"}, mode=InputMode.DRAFT_REVISION)
    findings = [{"type": "high_text_overlap", "message": "章节重合"}]
    v = assess_submittability(
        ws, terminated_reason="quality_met", originality_findings=findings
    )
    assert v.submittable is True
    assert v.cautions


def test_submittability_accuracy_met_counts_as_success():
    ws = _ws({"intro": "内容"}, mode=InputMode.DRAFT_REVISION)
    v = assess_submittability(ws, terminated_reason="accuracy_met")
    assert v.submittable is True


def test_submittability_agent_fabricated_citation_blocks_but_source_legacy_cautions():
    ws = _ws({"intro": "内容"}, mode=InputMode.DRAFT_REVISION)
    ws.quality_report = [
        {
            "type": "text_citation_invalid",
            "severity": "high",
            "section_id": "intro",
            "message": "新增伪造引用",
        },
        {
            "type": "source_citation_unverified",
            "severity": "high",
            "section_id": "intro",
            "message": "原稿未核验引用",
        },
    ]
    v = assess_submittability(ws, terminated_reason="accuracy_met")
    assert v.submittable is False
    assert any("Agent 新增" in b for b in v.blockers)
    assert any("source_citation_unverified" in c for c in v.cautions)


# --- 语言润色 ---


def test_polish_mock_is_noop():
    ws = _ws({"intro": "原始 [r1] 内容 有 数字 123.4"})
    agent = LanguagePolishAgent(_ScriptedLLM({}), is_mock=True)
    result = agent.run(AgentContext(workspace=ws))
    assert result.mutations == []


def test_polish_applies_when_guards_pass():
    original = "我们 提出 方法 [r1]，准确率 达到 95.6%。"
    # 润色版：保留 [r1] 与 95.6%，仅改语言。
    polished = "本文提出了一种方法 [r1]，其准确率达到 95.6%。"
    ws = _ws({"m": original})
    agent = LanguagePolishAgent(_ScriptedLLM({original: polished}), is_mock=False)
    result = agent.run(AgentContext(workspace=ws))
    for mut in result.mutations:
        mut(ws)
    assert ws.section_drafts["m"].content == polished


def test_polish_only_processes_sections_modified_this_round():
    first = "第一节保留数字 1。"
    first_polished = "第一节仍保留数字 1。"
    second = "第二节保留数字 2。"
    second_polished = "第二节仍保留数字 2。"
    ws = _ws({"first": first, "second": second})
    ws.profile["modified_section_ids"] = ["first"]
    agent = LanguagePolishAgent(
        _ScriptedLLM(
            {
                first: first_polished,
                second: second_polished,
            }
        ),
        is_mock=False,
    )
    result = agent.run(AgentContext(workspace=ws))
    for mutation in result.mutations:
        mutation(ws)
    assert ws.section_drafts["first"].content == first_polished
    assert ws.section_drafts["second"].content == second


def test_polish_rejects_when_citation_dropped():
    original = "我们提出方法 [r1]，准确率 95.6%。"
    bad = "我们提出方法，准确率 95.6%。"  # 丢了 [r1]
    ws = _ws({"m": original})
    agent = LanguagePolishAgent(_ScriptedLLM({original: bad}), is_mock=False)
    result = agent.run(AgentContext(workspace=ws))
    # 守卫拦截 → 无 mutation，原文保留。
    assert result.mutations == []
    assert ws.section_drafts["m"].content == original


def test_polish_rejects_when_number_changed():
    original = "准确率 95.6% 明显 提升。"
    bad = "准确率 96.5% 明显 提升。"  # 篡改数字
    ws = _ws({"m": original})
    agent = LanguagePolishAgent(_ScriptedLLM({original: bad}), is_mock=False)
    result = agent.run(AgentContext(workspace=ws))
    assert result.mutations == []
    assert ws.section_drafts["m"].content == original
