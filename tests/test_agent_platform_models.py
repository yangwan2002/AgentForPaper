"""agent_platform.models 单元测试（任务 1）。

覆盖：构造、默认值、序列化往返、Typesetting 的「未指定」语义与防御式解析、
transcript 记录的可序列化保证。
"""

from __future__ import annotations

from paper_agent.agent_platform.models import (
    ALIGNMENT_VALUES,
    AgentSession,
    GateOutcome,
    RejectedChange,
    TaskAgentConfig,
    TaskResult,
    ToolSpec,
    Typesetting,
    WritingTask,
)


# --- WritingTask -------------------------------------------------------------

def test_writing_task_has_instruction_true_for_nonblank():
    assert WritingTask(instruction="改一下引言").has_instruction() is True


def test_writing_task_has_instruction_false_for_blank():
    assert WritingTask(instruction="").has_instruction() is False
    assert WritingTask(instruction="   \n\t ").has_instruction() is False


def test_writing_task_defaults():
    task = WritingTask(instruction="x")
    assert task.workspace_id is None
    assert task.draft_path is None
    assert task.topic_background is None
    assert task.artifact is None
    assert task.profile == {}


# --- AgentSession.record -----------------------------------------------------

def test_agent_session_record_keeps_jsonish_values():
    session = AgentSession(session_id="ws1", workspace=object(), task=WritingTask("x"))
    session.record("tool_call", name="rewrite_section", ok=True, count=3)
    assert session.transcript == [
        {"kind": "tool_call", "name": "rewrite_section", "ok": True, "count": 3}
    ]


def test_agent_session_record_stringifies_non_jsonish():
    session = AgentSession(session_id="ws1", workspace=object(), task=WritingTask("x"))
    sentinel = object()
    session.record("decision", obj=sentinel)
    assert session.transcript[0]["obj"] == str(sentinel)


# --- TaskResult --------------------------------------------------------------

def test_task_result_to_dict_roundtrips_fields():
    result = TaskResult(
        session_id="ws1",
        summary="done",
        completed=["改了引言"],
        unfinished=["缺数据未核验"],
        guardrail_report={"faithfulness": "passed"},
        bound_hit="max_iters",
        export_files=["output/a.docx"],
    )
    data = result.to_dict()
    assert data["session_id"] == "ws1"
    assert data["completed"] == ["改了引言"]
    assert data["unfinished"] == ["缺数据未核验"]
    assert data["guardrail_report"] == {"faithfulness": "passed"}
    assert data["bound_hit"] == "max_iters"
    assert data["export_files"] == ["output/a.docx"]


def test_task_result_defaults():
    result = TaskResult(session_id="ws1")
    assert result.bound_hit is None
    assert result.completed == [] and result.unfinished == []


# --- TaskAgentConfig ---------------------------------------------------------

def test_task_agent_config_defaults():
    cfg = TaskAgentConfig()
    assert cfg.max_iters == 12
    assert cfg.context_token_budget == 32_000
    assert cfg.max_tool_result_tokens == 2_000
    assert cfg.keep_recent_turns == 3


# --- GateOutcome / RejectedChange -------------------------------------------

def test_gate_outcome_defaults_empty():
    outcome = GateOutcome(passed=True)
    assert outcome.accepted_mutations == []
    assert outcome.rejected == []
    assert outcome.notes == []


def test_rejected_change_fields():
    rc = RejectedChange(section_id="intro", reason="无支撑引用", dimension="faithfulness")
    assert rc.section_id == "intro"
    assert rc.dimension == "faithfulness"


# --- ToolSpec ----------------------------------------------------------------

def test_tool_spec_default_schema_is_empty_object():
    spec = ToolSpec(name="draw", description="画图")
    assert spec.parameters_schema == {"type": "object", "properties": {}}


# --- Typesetting -------------------------------------------------------------

def test_typesetting_empty_by_default():
    ts = Typesetting()
    assert ts.is_empty() is True
    assert ts.to_dict() == {}


def test_typesetting_to_dict_only_specified_fields():
    ts = Typesetting(line_spacing=22.0, alignment="justify")
    assert ts.is_empty() is False
    assert ts.to_dict() == {"line_spacing": 22.0, "alignment": "justify"}


def test_typesetting_from_dict_roundtrip():
    original = Typesetting(
        line_spacing=22.0, alignment="justify", first_line_indent="2ch", font="宋体"
    )
    restored = Typesetting.from_dict(original.to_dict())
    assert restored == original


def test_typesetting_from_dict_none_is_all_unspecified():
    assert Typesetting.from_dict(None).is_empty() is True
    assert Typesetting.from_dict({}).is_empty() is True


def test_typesetting_from_dict_rejects_invalid_alignment():
    ts = Typesetting.from_dict({"alignment": "diagonal"})
    assert ts.alignment is None


def test_typesetting_columns_roundtrip_and_is_empty():
    # columns 是一等排版原语：非空时 is_empty=False，序列化往返一致。
    ts = Typesetting(columns=2)
    assert ts.is_empty() is False
    assert ts.to_dict() == {"columns": 2}
    assert Typesetting.from_dict(ts.to_dict()) == ts


def test_typesetting_from_dict_rejects_invalid_columns():
    # 防御式：非整数/小于 1 的分栏数视为未指定，不因脏数据破坏导出。
    assert Typesetting.from_dict({"columns": 0}).columns is None
    assert Typesetting.from_dict({"columns": -3}).columns is None
    assert Typesetting.from_dict({"columns": "abc"}).columns is None
    assert Typesetting.from_dict({"columns": 2}).columns == 2


def test_typesetting_alignment_values_constant():
    assert set(ALIGNMENT_VALUES) == {"left", "center", "right", "justify"}
