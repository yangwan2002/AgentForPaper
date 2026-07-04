"""澄清问答测试：Elicitor 三实现 + 确定性修订范围/缺失章节决策 + 编排器接入。"""

from __future__ import annotations

from paper_agent.clarification import (
    SCOPE_FULL,
    SCOPE_LANGUAGE,
    SCOPE_STRUCTURE,
    RevisionScope,
    clarify_revision_scope,
    missing_canonical,
)
from paper_agent.elicitation import (
    AutoElicitor,
    CLIElicitor,
    Question,
    ScriptedElicitor,
)
from paper_agent.prompts.section_types import SectionType


# --- Elicitor ---


def test_auto_elicitor_returns_default():
    e = AutoElicitor()
    assert e.ask(Question("q", "?", options=["a", "b"], default="b")) == "b"
    assert e.ask(Question("q2", "?")) == ""


def test_scripted_elicitor_by_id_and_default():
    e = ScriptedElicitor({"q1": "hello"})
    assert e.ask(Question("q1", "?")) == "hello"
    # 未命中 → 默认。
    assert e.ask(Question("q2", "?", default="d")) == "d"


def test_scripted_elicitor_queue():
    e = ScriptedElicitor(["one", "two"])
    assert e.ask(Question("a", "?")) == "one"
    assert e.ask(Question("b", "?")) == "two"
    assert e.ask(Question("c", "?", default="def")) == "def"


def test_cli_elicitor_number_selection_and_empty_default():
    inputs = iter(["2", ""])
    e = CLIElicitor(input_fn=lambda _p: next(inputs), output_fn=lambda _m: None)
    q = Question("q", "?", options=["x", "y", "z"], default="z")
    assert e.ask(q) == "y"           # 选序号 2
    assert e.ask(q) == "z"           # 空输入 → 默认


def test_cli_elicitor_freetext():
    e = CLIElicitor(input_fn=lambda _p: "自由文本", output_fn=lambda _m: None)
    assert e.ask(Question("q", "?")) == "自由文本"


# --- 缺失章节检测 ---


def test_missing_canonical_detects_gaps():
    # 只有方法+实验 → 缺 引言/相关工作/结论。
    present = [("method", "方法"), ("exp", "实验")]
    missing = missing_canonical(present)
    types = {t for t, _name in missing}
    assert SectionType.INTRODUCTION in types
    assert SectionType.CONCLUSION in types
    assert SectionType.RELATED_WORK in types


def test_missing_canonical_none_when_present():
    present = [
        ("intro", "引言"),
        ("related", "相关工作"),
        ("concl", "结论"),
        ("method", "方法"),
    ]
    assert missing_canonical(present) == []


# --- 范围澄清 ---


def test_clarify_scope_language_only_default():
    scope = clarify_revision_scope(AutoElicitor(), [("method", "方法")])
    assert scope.polish_language is True
    assert scope.add_missing_sections is False
    assert scope.add_citations is False
    assert scope.sections_to_add == []


def test_clarify_scope_structure_adds_selected_sections():
    e = ScriptedElicitor(
        {
            "revision_scope": SCOPE_STRUCTURE,
            "add_section_introduction": "是",
            "add_section_related_work": "否",
            "add_section_conclusion": "是",
        }
    )
    scope = clarify_revision_scope(e, [("method", "方法"), ("exp", "实验")])
    assert scope.add_missing_sections is True
    assert scope.add_citations is False
    assert set(scope.sections_to_add) == {"introduction", "conclusion"}


def test_clarify_scope_full_sets_citations():
    e = ScriptedElicitor(
        {"revision_scope": SCOPE_FULL, "add_section_introduction": "是"}
    )
    scope = clarify_revision_scope(e, [("method", "方法")])
    assert scope.add_citations is True


def test_revision_scope_roundtrip():
    scope = RevisionScope(
        add_missing_sections=True, add_citations=True, sections_to_add=["introduction"]
    )
    assert RevisionScope.from_dict(scope.to_dict()) == scope


# --- 编排器接入（草稿修订）---


def test_orchestrator_clarify_adds_sections():
    from paper_agent.app import build_orchestrator
    from paper_agent.config import Config
    from paper_agent.orchestrator import PaperRequest
    from paper_agent.providers.llm.mock import MockLLMProvider
    from paper_agent.providers.retrieval.mock import MockRetrievalProvider

    # 用户选择补全引言/结论。
    elicitor = ScriptedElicitor(
        {
            "revision_scope": SCOPE_STRUCTURE,
            "add_section_introduction": "是",
            "add_section_related_work": "否",
            "add_section_conclusion": "是",
        }
    )
    orch = build_orchestrator(
        llm=MockLLMProvider(),
        retrieval=MockRetrievalProvider(),
        config=Config(iteration_limit=1, workspace_dir=".paper_ws_test_clarify"),
        elicitor=elicitor,
    )
    draft = "# 方法\n我们提出 X。\n# 实验\n在数据集上评估。\n"
    result = orch.run(PaperRequest(draft=draft))
    ws = orch._repo.load(result.workspace_id)
    section_ids = {n.section_id for n in ws.outline}
    # 澄清补入的章节应出现在大纲中。
    assert "introduction" in section_ids
    assert "conclusion" in section_ids
    assert "related_work" not in section_ids
    # 决策已记录，续跑不重复问。
    assert ws.profile.get("revision_scope", {}).get("add_missing_sections") is True


def test_orchestrator_auto_elicitor_no_structural_change():
    """非交互（默认 AutoElicitor）：草稿修订不新增任何章节（向后兼容）。"""
    from paper_agent.app import build_orchestrator
    from paper_agent.config import Config
    from paper_agent.orchestrator import PaperRequest
    from paper_agent.providers.llm.mock import MockLLMProvider
    from paper_agent.providers.retrieval.mock import MockRetrievalProvider

    orch = build_orchestrator(
        llm=MockLLMProvider(),
        retrieval=MockRetrievalProvider(),
        config=Config(iteration_limit=1, workspace_dir=".paper_ws_test_clarify2"),
    )
    draft = "# 方法\n我们提出 X。\n# 实验\n在数据集上评估。\n"
    result = orch.run(PaperRequest(draft=draft))
    ws = orch._repo.load(result.workspace_id)
    section_ids = {n.section_id for n in ws.outline}
    assert "introduction" not in section_ids
    assert "conclusion" not in section_ids


# --- 动态澄清问题（路径 B：LLM 提出，受约束）---


class _StubParser:
    """StructuredParser 桩：返回预置 ParseOutcome。"""

    def __init__(self, status, data=None, reason=""):
        from paper_agent.parsing.structured_parser import ParseOutcome

        self._outcome = ParseOutcome(status=status, data=data, reason=reason)

    def request_json(self, messages, *, required_keys=(), **kw):
        return self._outcome


def _ws_generation(topic="多智能体协作"):
    from paper_agent.workspace.models import InputMode, OutlineNode, PaperWorkspace

    ws = PaperWorkspace(workspace_id="wsg", input_mode=InputMode.GENERATION,
                        topic_background=topic)
    ws.outline.append(OutlineNode(section_id="method", title="方法", order=0))
    return ws


def test_proposer_returns_bounded_questions():
    from paper_agent.clarification import ClarificationProposer
    from paper_agent.workspace.models import ParseStatus

    data = {
        "questions": [
            {"id": "q_venue", "prompt": "目标会议是？", "options": ["NeurIPS", "ICML"]},
            {"id": "q_scope", "prompt": "是否包含理论证明？"},
            {"id": "q3", "prompt": "第三问"},
            {"id": "q4", "prompt": "第四问（应被截断）"},
        ]
    }
    proposer = ClarificationProposer(_StubParser(ParseStatus.PARSED, data), max_questions=2)
    qs = proposer.propose(_ws_generation())
    assert len(qs) == 2
    assert qs[0].id == "q_venue"
    assert qs[0].options == ["NeurIPS", "ICML"]


def test_proposer_non_parsed_returns_empty():
    from paper_agent.clarification import ClarificationProposer
    from paper_agent.workspace.models import ParseStatus

    proposer = ClarificationProposer(_StubParser(ParseStatus.FAILED), max_questions=3)
    assert proposer.propose(_ws_generation()) == []


def test_proposer_max_zero_returns_empty_without_calling():
    from paper_agent.clarification import ClarificationProposer
    from paper_agent.workspace.models import ParseStatus

    called = {"n": 0}

    class _Spy(_StubParser):
        def request_json(self, *a, **k):
            called["n"] += 1
            return super().request_json(*a, **k)

    proposer = ClarificationProposer(
        _Spy(ParseStatus.PARSED, {"questions": []}), max_questions=0
    )
    assert proposer.propose(_ws_generation()) == []
    assert called["n"] == 0


def test_orchestrator_llm_clarify_records_answers():
    """交互式 Elicitor + 注入提出器 → 问答被记录进 ws.profile 并注入写作偏好。"""
    from paper_agent.app import build_orchestrator
    from paper_agent.clarification import ClarificationProposer
    from paper_agent.config import Config
    from paper_agent.orchestrator import PaperRequest
    from paper_agent.providers.llm.mock import MockLLMProvider
    from paper_agent.providers.retrieval.mock import MockRetrievalProvider
    from paper_agent.workspace.models import ParseStatus

    proposer = ClarificationProposer(
        _StubParser(
            ParseStatus.PARSED,
            {"questions": [{"id": "q_venue", "prompt": "目标会议是？",
                            "options": ["NeurIPS", "ICML"], "default": "NeurIPS"}]},
        ),
        max_questions=3,
    )
    elicitor = ScriptedElicitor({"q_venue": "ICML"})
    orch = build_orchestrator(
        llm=MockLLMProvider(),
        retrieval=MockRetrievalProvider(),
        config=Config(iteration_limit=1, workspace_dir=".paper_ws_llmclar"),
        elicitor=elicitor,
    )
    orch._clarify_proposer = proposer  # 直接注入（默认 config 未开启装配）
    result = orch.run(PaperRequest(topic_background="多智能体协作"))
    ws = orch._repo.load(result.workspace_id)
    answers = ws.profile.get("clarification_answers")
    assert answers and answers[0]["answer"] == "ICML"


def test_orchestrator_llm_clarify_skipped_when_non_interactive():
    """非交互（AutoElicitor）→ 即便注入提出器也不提问、不记录。"""
    from paper_agent.app import build_orchestrator
    from paper_agent.clarification import ClarificationProposer
    from paper_agent.config import Config
    from paper_agent.orchestrator import PaperRequest
    from paper_agent.providers.llm.mock import MockLLMProvider
    from paper_agent.providers.retrieval.mock import MockRetrievalProvider
    from paper_agent.workspace.models import ParseStatus

    calls = {"n": 0}

    class _Spy(_StubParser):
        def request_json(self, *a, **k):
            calls["n"] += 1
            return super().request_json(*a, **k)

    proposer = ClarificationProposer(
        _Spy(ParseStatus.PARSED, {"questions": [{"id": "q", "prompt": "?"}]}),
        max_questions=3,
    )
    orch = build_orchestrator(
        llm=MockLLMProvider(),
        retrieval=MockRetrievalProvider(),
        config=Config(iteration_limit=1, workspace_dir=".paper_ws_llmclar2"),
    )  # 默认 AutoElicitor
    orch._clarify_proposer = proposer
    result = orch.run(PaperRequest(topic_background="多智能体协作"))
    ws = orch._repo.load(result.workspace_id)
    assert ws.profile.get("clarification_answers") is None
    assert calls["n"] == 0  # 非交互下连提出器都不调用
