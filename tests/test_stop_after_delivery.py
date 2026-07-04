"""机制级 harness「交付即停」测试：产出交付物后强制收尾，不再擅自扩展。"""

from __future__ import annotations

from paper_agent.agent_platform.models import AgentSession, WritingTask
from paper_agent.agent_platform.task_agent import TaskAgent
from paper_agent.providers.llm.base import LLMResponse, ToolCall
from paper_agent.tools.registry import ToolRegistry
from paper_agent.workspace.models import InputMode, PaperWorkspace


def _session():
    ws = PaperWorkspace(workspace_id="w1", input_mode=InputMode.DRAFT_REVISION)
    return AgentSession(session_id="w1", workspace=ws, task=WritingTask("转格式"))


class _ScriptedLLM:
    """先调交付类工具（convert_document），若循环继续则会调 read_section（越界）。"""

    def __init__(self):
        self._i = 0

    def complete(self, messages, **opts):
        self._i += 1
        if self._i == 1:
            return LLMResponse(content="", tool_calls=[ToolCall(
                id="c1", name="convert_document", arguments={"to_format": "docx"})])
        if self._i == 2:
            # 若没被「交付即停」拦住，agent 会在这里越界去排查。
            return LLMResponse(content="", tool_calls=[ToolCall(
                id="c2", name="read_section", arguments={"section_id": "s1"})])
        return LLMResponse(content="收尾。")


def _registry(*, convert_produces_file: bool):
    reg = ToolRegistry()
    reg.register(
        "convert_document", "转换",
        lambda to_format=None, **k: "已转换" if convert_produces_file else "失败",
        {"type": "object", "properties": {"to_format": {"type": "string"}}, "required": []},
    )
    reg.register(
        "read_section", "读章节", lambda section_id=None: f"内容:{section_id}",
        {"type": "object", "properties": {"section_id": {"type": "string"}}, "required": []},
    )
    return reg


def _agent_with_recording_convert(monkeypatch, agent, produces_file: bool):
    """给交付类工具的执行补记一条带 files 的 transcript（模拟真实工具 record(files=...)）。"""
    orig = agent._execute_tool

    def _exec(sess, messages, call):
        orig(sess, messages, call)
        if call.name == "convert_document" and produces_file:
            sess.record("convert_document", files=["output/x.docx"])

    monkeypatch.setattr(agent, "_execute_tool", _exec)


def _tool_names(session):
    return [e.get("name") for e in session.transcript if e.get("kind") == "tool_call"]


def test_stops_after_delivery(monkeypatch):
    """convert_document 产出文件后，本轮强制收尾，read_section 不被调用。"""
    session = _session()
    agent = TaskAgent(_ScriptedLLM(), _registry(convert_produces_file=True),
                      stop_after_delivery=True)
    _agent_with_recording_convert(monkeypatch, agent, produces_file=True)

    agent.run(session)
    names = _tool_names(session)
    assert "convert_document" in names
    assert "read_section" not in names  # 交付即停：未越界调用


def test_no_stop_when_disabled(monkeypatch):
    """关闭机制时恢复旧行为：交付后仍会继续（read_section 被调用）。"""
    session = _session()
    agent = TaskAgent(_ScriptedLLM(), _registry(convert_produces_file=True),
                      stop_after_delivery=False)
    _agent_with_recording_convert(monkeypatch, agent, produces_file=True)

    agent.run(session)
    assert "read_section" in _tool_names(session)  # 未启用机制 → 旧行为，继续扩展


def test_no_stop_when_delivery_failed(monkeypatch):
    """交付类工具失败（无产出文件）时不停，留给 agent 继续（重试/改路径）。"""
    session = _session()
    agent = TaskAgent(_ScriptedLLM(), _registry(convert_produces_file=False),
                      stop_after_delivery=True)
    # 不补记 files（模拟失败：无产出）。
    _agent_with_recording_convert(monkeypatch, agent, produces_file=False)

    agent.run(session)
    # 交付失败 → 不触发停止 → 进入下一轮，read_section 被调用。
    assert "read_section" in _tool_names(session)
