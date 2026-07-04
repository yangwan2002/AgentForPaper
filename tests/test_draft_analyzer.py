"""draft_analyzer + 批量澄清（collect_clarification_questions / apply_clarification_answers）
的单元测试。

覆盖：
- ``analyze_draft`` 各缺口维度的检出与不误报；
- ``collect_clarification_questions`` 据缺口构造一批 Question；
- ``apply_clarification_answers`` 把 ``ask_batch`` 答案汇成 ``RevisionScope`` + 偏好；
- 无缺口时不提问（非交互零影响）。
"""

from __future__ import annotations

from paper_agent.clarification import (
    SCOPE_LANGUAGE,
    SCOPE_STRUCTURE,
    apply_clarification_answers,
    build_inplace_reroute_question,
    collect_clarification_questions,
)
from paper_agent.draft_analyzer import DraftGaps, analyze_draft, analyze_text
from paper_agent.elicitation import AutoElicitor, ScriptedElicitor
from paper_agent.workspace.models import (
    InputMode,
    OutlineNode,
    OutputFormat,
    PaperWorkspace,
)


def _make_ws(
    *,
    draft: str = "",
    outline: list[OutlineNode] | None = None,
    output_format: OutputFormat = OutputFormat.MARKDOWN,
    input_mode: InputMode = InputMode.DRAFT_REVISION,
    input_path: str = "",
) -> PaperWorkspace:
    ws = PaperWorkspace(
        workspace_id="t",
        input_mode=input_mode,
        output_format=output_format,
        original_draft=draft,
    )
    ws.outline = outline or []
    ws.profile = {"input_path": input_path} if input_path else {}
    return ws


# --- analyze_draft 各维度 ---


def test_analyze_no_gap_when_generation_mode_with_empty_draft():
    ws = _make_ws(input_mode=InputMode.GENERATION, draft="")
    gaps = analyze_draft(ws)
    assert not gaps.any_gap()
    assert gaps.missing_sections == []


def test_analyze_detects_missing_sections_in_draft_revision():
    ws = _make_ws(
        draft="正文",
        outline=[OutlineNode(section_id="method", title="方法", order=1.0)],
    )
    gaps = analyze_draft(ws)
    values = [v for (v, _name) in gaps.missing_sections]
    assert "introduction" in values
    assert "related_work" in values
    assert "conclusion" in values


def test_analyze_no_missing_sections_when_all_canonical_present():
    ws = _make_ws(
        draft="正文",
        outline=[
            OutlineNode(section_id="intro", title="Introduction", order=1.0),
            OutlineNode(section_id="rw", title="Related Work", order=2.0),
            OutlineNode(section_id="concl", title="Conclusion", order=3.0),
        ],
    )
    gaps = analyze_draft(ws)
    assert gaps.missing_sections == []


def test_analyze_detects_missing_reference_list_when_citations_but_no_heading():
    ws = _make_ws(draft="如 [1] 所述，[2] 提出该方法。")
    gaps = analyze_draft(ws)
    assert gaps.missing_reference_list is True


def test_analyze_no_missing_reference_list_when_heading_present():
    ws = _make_ws(draft="如 [1] 所述。\n\n# References\n[1] Foo.")
    gaps = analyze_draft(ws)
    assert gaps.missing_reference_list is False


def test_analyze_detects_numeric_claims_without_artifact():
    ws = _make_ws(draft="我们的方法 F1=0.87，提升 +3.2%，p<0.01。")
    gaps = analyze_draft(ws)
    assert gaps.numeric_claims_without_artifact is True


def test_analyze_no_numeric_claims_when_no_numbers():
    ws = _make_ws(draft="本文提出一种新方法。")
    gaps = analyze_draft(ws)
    assert gaps.numeric_claims_without_artifact is False


def test_analyze_detects_output_format_mismatch_tex_to_docx():
    ws = _make_ws(
        draft="正文", output_format=OutputFormat.DOCX, input_path="main.tex"
    )
    gaps = analyze_draft(ws, input_ext=".tex")
    assert gaps.output_format_mismatch is True
    assert "LaTeX" in gaps.output_format_hint


def test_analyze_no_output_mismatch_when_tex_to_tex():
    ws = _make_ws(
        draft="正文", output_format=OutputFormat.LATEX, input_path="main.tex"
    )
    gaps = analyze_draft(ws, input_ext=".tex")
    assert gaps.output_format_mismatch is False


# --- collect + apply 批量澄清 ---


def test_collect_questions_covers_all_gaps():
    ws = _make_ws(
        draft="方法 F1=0.87，[1] 提出该方法。",
        outline=[OutlineNode(section_id="method", title="方法", order=1.0)],
        output_format=OutputFormat.DOCX,
        input_path="main.tex",
    )
    gaps = analyze_draft(ws, input_ext=".tex")
    batch = collect_clarification_questions(gaps)
    qids = [q.id for q in batch.questions]
    assert "revision_scope" in qids
    assert "add_section_introduction" in qids
    assert "add_section_related_work" in qids
    assert "add_section_conclusion" in qids
    assert "missing_refs" in qids
    assert "numeric_claims" in qids
    assert "output_format" in qids


def test_collect_questions_empty_when_no_gap_and_no_scope():
    gaps = DraftGaps()
    batch = collect_clarification_questions(gaps, include_scope=False)
    assert batch.questions == []


def test_apply_answers_language_only_default():
    gaps = DraftGaps(
        missing_sections=[("introduction", "引言"), ("conclusion", "结论")]
    )
    batch = collect_clarification_questions(gaps)
    # 非交互：全部取默认 → 仅语言润色、不补章节。
    answers = AutoElicitor().ask_batch(batch.questions)
    scope, prefs = apply_clarification_answers(batch, answers)
    assert scope.polish_language is True
    assert scope.add_missing_sections is False
    assert scope.sections_to_add == []
    assert prefs == {}


def test_apply_answers_structure_adds_selected_sections():
    gaps = DraftGaps(
        missing_sections=[("introduction", "引言"), ("conclusion", "结论")]
    )
    batch = collect_clarification_questions(gaps)
    el = ScriptedElicitor(
        {
            "revision_scope": SCOPE_STRUCTURE,
            "add_section_introduction": "是",
            "add_section_conclusion": "否",
        }
    )
    answers = el.ask_batch(batch.questions)
    scope, _prefs = apply_clarification_answers(batch, answers)
    assert scope.add_missing_sections is True
    assert scope.sections_to_add == ["introduction"]


def test_apply_answers_records_preferences_for_non_scope_gaps():
    gaps = DraftGaps(
        missing_reference_list=True,
        numeric_claims_without_artifact=True,
        output_format_mismatch=True,
        output_format_hint="冲突说明",
    )
    batch = collect_clarification_questions(gaps, include_scope=False)
    el = ScriptedElicitor(
        {
            "missing_refs": "系统据 [id] 检索并补全参考文献段",
            "numeric_claims": "信任正文数字（不核验）",
            "output_format": "改回与输入一致",
        }
    )
    answers = el.ask_batch(batch.questions)
    _scope, prefs = apply_clarification_answers(batch, answers)
    assert "missing_refs" in prefs
    assert "numeric_claims" in prefs
    assert prefs["output_format"].startswith("改回与输入一致")


def test_apply_answers_scope_structure_but_all_sections_no_falls_back():
    """用户选了「补章节」范围但每章都选否 → 回落为无结构改动（与旧逻辑一致）。"""
    gaps = DraftGaps(missing_sections=[("introduction", "引言")])
    batch = collect_clarification_questions(gaps)
    el = ScriptedElicitor(
        {"revision_scope": SCOPE_STRUCTURE, "add_section_introduction": "否"}
    )
    answers = el.ask_batch(batch.questions)
    scope, _prefs = apply_clarification_answers(batch, answers)
    assert scope.add_missing_sections is False
    assert scope.sections_to_add == []


# --- analyze_text（文本级，in-place 路径用）---


def test_analyze_text_detects_missing_sections_from_titles():
    gaps = analyze_text(
        "正文",
        titles=[("method", "方法")],
        has_artifact=True,
    )
    values = [v for (v, _n) in gaps.missing_sections]
    assert "introduction" in values
    assert "conclusion" in values


def test_analyze_text_skips_sections_when_check_sections_false():
    """in-place 路径无章节信息时可跳过章节检测。"""
    gaps = analyze_text("正文", titles=None, check_sections=False)
    assert gaps.missing_sections == []


def test_analyze_text_no_artifact_flag_when_has_artifact_true():
    """有 artifact 时即使含数字声明也不标记缺口。"""
    gaps = analyze_text("F1=0.87", has_artifact=True)
    assert gaps.numeric_claims_without_artifact is False


def test_analyze_text_detects_numeric_when_no_artifact():
    gaps = analyze_text("F1=0.87，+3.2%", has_artifact=False)
    assert gaps.numeric_claims_without_artifact is True


def test_analyze_text_detects_output_mismatch():
    from paper_agent.workspace.models import OutputFormat

    gaps = analyze_text(
        "正文",
        input_ext=".tex",
        output_format=OutputFormat.DOCX,
        check_sections=False,
    )
    assert gaps.output_format_mismatch is True


# --- build_inplace_reroute_question ---


def test_inplace_reroute_question_none_when_no_gap():
    gaps = DraftGaps()
    assert build_inplace_reroute_question(gaps) is None


def test_inplace_reroute_question_ignores_output_mismatch():
    """output_format_mismatch 对 in-place 无意义（输出=输入），不应触发 reroute。"""
    gaps = DraftGaps(output_format_mismatch=True, output_format_hint="冲突")
    assert build_inplace_reroute_question(gaps) is None


def test_inplace_reroute_question_for_missing_sections():
    gaps = DraftGaps(missing_sections=[("introduction", "引言"), ("conclusion", "结论")])
    q = build_inplace_reroute_question(gaps)
    assert q is not None
    assert q.id == "inplace_vs_rebuild"
    assert "缺常规章节" in q.prompt
    assert "引言" in q.prompt and "结论" in q.prompt
    assert q.default.startswith("继续原地润色")  # 最保守默认


def test_inplace_reroute_question_for_multiple_gaps():
    gaps = DraftGaps(
        missing_sections=[("introduction", "引言")],
        missing_reference_list=True,
        numeric_claims_without_artifact=True,
    )
    q = build_inplace_reroute_question(gaps)
    assert q is not None
    assert "缺常规章节" in q.prompt
    assert "参考文献段" in q.prompt
    assert "实验数字" in q.prompt


def test_inplace_reroute_default_is_continue_inplace():
    """非交互下取默认 → 继续 in-place（reroute 返回 False）。"""
    from paper_agent.elicitation import AutoElicitor

    gaps = DraftGaps(missing_sections=[("introduction", "引言")])
    q = build_inplace_reroute_question(gaps)
    ans = AutoElicitor().ask(q)
    assert not ans.startswith("改走完整管线")  # 继续 in-place
