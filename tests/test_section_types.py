"""Round 5 修复回归测试：section-typed prompts + quality gate 必备元素检查。"""

from __future__ import annotations

from paper_agent.agents.base import AgentContext
from paper_agent.agents.writing_agent import WritingAgent
from paper_agent.context.manager import ContextManager
from paper_agent.prompts import templates
from paper_agent.prompts.section_types import (
    SPECS,
    SectionType,
    SectionTypeSpec,
    get_spec,
    infer_and_get_spec,
    infer_section_type,
)
from paper_agent.providers.llm.mock import MockLLMProvider
from paper_agent.tools.quality_gate import QualityGate
from paper_agent.workspace.models import (
    InputMode,
    OutlineNode,
    PaperWorkspace,
    SectionDraft,
)


# --------------------------------------------------------------------------- #
# section_types.infer_section_type
# --------------------------------------------------------------------------- #


def test_infer_section_type_by_section_id():
    assert infer_section_type("introduction", "引言") is SectionType.INTRODUCTION
    assert infer_section_type("related_work", "相关工作") is SectionType.RELATED_WORK
    assert infer_section_type("method", "方法") is SectionType.METHOD
    assert infer_section_type("experiments", "实验") is SectionType.EXPERIMENTS
    assert infer_section_type("conclusion", "结论") is SectionType.CONCLUSION
    assert infer_section_type("limitations", "局限") is SectionType.LIMITATIONS


def test_infer_section_type_by_title_substring():
    # title 子串匹配（小写）。
    assert infer_section_type("sec_3", "Methodology") is SectionType.METHOD
    assert infer_section_type("sec_2", "Literature Review") is SectionType.RELATED_WORK
    assert infer_section_type("sec_1", "Introduction") is SectionType.INTRODUCTION


def test_infer_section_type_unknown_when_no_match():
    assert infer_section_type("sec_x", "杂项") is SectionType.UNKNOWN


def test_infer_section_type_specific_over_generic():
    """'related work' 应优先于 'work' 误命中其他类型，验证顺序。"""
    assert (
        infer_section_type("related_work", "Related Work")
        is SectionType.RELATED_WORK
    )


def test_get_spec_returns_unknown_for_unregistered():
    spec = get_spec(SectionType.UNKNOWN)
    assert spec.type is SectionType.UNKNOWN
    assert spec.writing_guidance  # 非空


def test_all_section_types_have_spec():
    for st in SectionType:
        assert isinstance(SPECS[st], SectionTypeSpec)
        assert SPECS[st].type is st
        assert SPECS[st].writing_guidance


# --------------------------------------------------------------------------- #
# writing prompt 注入 section_guidance
# --------------------------------------------------------------------------- #


def test_writing_section_includes_section_guidance():
    spec = infer_and_get_spec("method", "方法")
    messages = templates.writing_section(
        title="方法",
        hint="",
        run_context="ctx",
        summaries="",
        section_guidance=spec.writing_guidance,
    )
    task_msg = messages[-1].content
    assert "章节体裁规约" in task_msg
    assert "超参" in task_msg or "可复现" in task_msg


def test_writing_section_omits_guidance_when_empty():
    """空 section_guidance 不应在 prompt 里留空规约段。"""
    messages = templates.writing_section(
        title="X", hint="", run_context="ctx", summaries=""
    )
    task_msg = messages[-1].content
    assert "章节体裁规约" not in task_msg


def test_revise_section_includes_section_guidance():
    spec = infer_and_get_spec("limitations", "Limitations")
    messages = templates.revise_section(
        title="Limitations",
        suggestion="补充具体局限",
        content="原文",
        run_context="ctx",
        section_guidance=spec.writing_guidance,
    )
    task_msg = messages[-1].content
    assert "章节体裁规约" in task_msg
    assert "诚实" in task_msg or "局限" in task_msg


# --------------------------------------------------------------------------- #
# WritingAgent: section spec 端到端注入
# --------------------------------------------------------------------------- #


def test_writing_agent_writes_method_section_with_guidance():
    """WritingAgent 调 LLM 时，最后一条 user message 应含 method 的规约。"""
    llm = MockLLMProvider(scripted=["方法章节正文"])
    agent = WritingAgent(llm, ContextManager(MockLLMProvider()))
    ws = PaperWorkspace(
        workspace_id="w", input_mode=InputMode.GENERATION, topic_background="x"
    )
    ws.outline = [OutlineNode(section_id="method", title="方法", order=0)]
    result = agent.run(AgentContext(workspace=ws))
    for m in result.mutations:
        m(ws)
    # MockLLM 记录的 messages：最后一次调用（写章节）的最后一条 user 应含方法规约。
    last_call_messages = llm.calls[0]
    last_user = last_call_messages[-1].content
    assert "章节体裁规约" in last_user
    # 方法规约里的关键短语之一。
    assert any(kw in last_user for kw in ("超参", "可复现", "数据预处理"))


def test_writing_agent_uses_unknown_spec_for_unrecognized_section():
    """无法识别类型的章节用 UNKNOWN 规约，仍能正常写作。"""
    llm = MockLLMProvider(scripted=["杂项章节正文"])
    agent = WritingAgent(llm, ContextManager(MockLLMProvider()))
    ws = PaperWorkspace(
        workspace_id="w", input_mode=InputMode.GENERATION, topic_background="x"
    )
    ws.outline = [OutlineNode(section_id="appendix_x", title="附录 X", order=0)]
    result = agent.run(AgentContext(workspace=ws))
    for m in result.mutations:
        m(ws)
    assert ws.section_drafts["appendix_x"].content == "杂项章节正文"


# --------------------------------------------------------------------------- #
# QualityGate: 必备元素检查
# --------------------------------------------------------------------------- #


def _ws_with_method(content: str) -> PaperWorkspace:
    ws = PaperWorkspace(
        workspace_id="w", input_mode=InputMode.GENERATION, topic_background="x"
    )
    ws.outline = [OutlineNode(section_id="method", title="方法", order=0)]
    ws.section_drafts = {
        "method": SectionDraft(section_id="method", title="方法", content=content)
    }
    return ws


def test_quality_gate_flags_method_missing_hyperparameters():
    """方法章节缺超参 → 必备元素缺失（high severity）。"""
    # 含定义/符号但缺超参。
    content = (
        "本文定义如下问题。给定输入 x ∈ R^d，输出 y ∈ R。"
        "符号 notation 详见附录。" * 5
    )
    ws = _ws_with_method(content)
    report = QualityGate().check(ws)
    types = {i["type"] for i in report.issues}
    assert "missing_required_element" in types
    # 缺的是 hyperparameter。
    assert any(
        i["type"] == "missing_required_element" and "hyperparameter" in i["message"]
        for i in report.issues
    )
    assert report.passed is False


def test_quality_gate_method_with_all_required_elements_passes():
    """方法章节包含所有必备元素 → 通过必备元素检查。"""
    content = (
        "本文定义如下问题：给定 x ∈ R^d。"
        "训练用学习率 0.001、batch size 32、Adam 优化器。" * 5
    )
    ws = _ws_with_method(content)
    report = QualityGate().check(ws)
    # 不应有 missing_required_element 类的 issue。
    assert not any(
        i["type"] == "missing_required_element" for i in report.issues
    )


def test_quality_gate_unknown_section_skips_required_elements():
    """未识别的章节类型（UNKNOWN）无必备元素 → 不检查。"""
    ws = PaperWorkspace(
        workspace_id="w", input_mode=InputMode.GENERATION, topic_background="x"
    )
    ws.outline = [OutlineNode(section_id="misc", title="杂项", order=0)]
    ws.section_drafts = {
        "misc": SectionDraft(
            section_id="misc", title="杂项",
            content="充分展开的杂项章节正文。" * 20,
        )
    }
    report = QualityGate().check(ws)
    assert not any(
        i["type"] == "missing_required_element" for i in report.issues
    )


def test_quality_gate_experiments_missing_dataset_and_baseline():
    """实验章节缺数据集与基线 → 两条必备元素 high。"""
    ws = PaperWorkspace(
        workspace_id="w", input_mode=InputMode.GENERATION, topic_background="x"
    )
    ws.outline = [OutlineNode(section_id="experiments", title="实验", order=0)]
    ws.section_drafts = {
        "experiments": SectionDraft(
            section_id="experiments", title="实验",
            # 含 metric 但缺 dataset 与 baseline。
            content="我们汇报准确率 accuracy 与 F1 指标。" * 10,
        )
    }
    report = QualityGate().check(ws)
    missing = [
        i for i in report.issues if i["type"] == "missing_required_element"
    ]
    categories = {
        msg
        for i in missing
        for msg in [i["message"]]
    }
    # 应该至少有 dataset 与 baseline 两条缺失。
    assert any("dataset" in m for m in categories)
    assert any("baseline" in m for m in categories)
    assert report.passed is False
