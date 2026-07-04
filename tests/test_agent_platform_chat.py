"""交互式对话层测试（路径 A）：多轮连续性、ChatController、REPL。"""

from __future__ import annotations

import copy

from paper_agent.agent_platform.app import PaperAgentApp
from paper_agent.agent_platform.chat import ChatController, run_chat_repl
from paper_agent.agent_platform.guardrail_gate import GuardrailGate
from paper_agent.agent_platform.models import AgentSession, WritingTask
from paper_agent.agent_platform.task_agent import TaskAgent
from paper_agent.providers.llm.base import LLMResponse, ToolCall
from paper_agent.tools.citation import CitationVerifier
from paper_agent.tools.registry import ToolRegistry
from paper_agent.workspace.models import (
    InputMode,
    OutlineNode,
    PaperWorkspace,
    SectionDraft,
)
from paper_agent.workspace.repository import WorkspaceRepository


class _MemStore:
    def __init__(self):
        self._data = {}

    def load(self, wid):
        raw = self._data.get(wid)
        return PaperWorkspace.from_dict(raw) if raw else None

    def save(self, ws):
        self._data[ws.workspace_id] = copy.deepcopy(ws.to_dict())


class _ScriptedLLM:
    """按轮返回；记录每次收到的 messages 以便断言上下文连续性。"""

    def __init__(self, script):
        self._script = list(script)
        self._i = 0
        self.seen_messages = []

    def complete(self, messages, **opts):
        self.seen_messages.append(list(messages))
        if self._i < len(self._script):
            r = self._script[self._i]
            self._i += 1
            return r
        return LLMResponse(content="ok")


def _session():
    ws = PaperWorkspace(workspace_id="w1", input_mode=InputMode.DRAFT_REVISION)
    ws.outline = [OutlineNode(section_id="intro", title="引言", order=0)]
    ws.section_drafts = {"intro": SectionDraft(section_id="intro", title="引言", content="旧")}
    return AgentSession(session_id="w1", workspace=ws, task=WritingTask(""))


def _repo(ws):
    repo = WorkspaceRepository(_MemStore())
    repo.create(ws)
    return repo


# --- 多轮连续性 --------------------------------------------------------------

def test_converse_accumulates_context_across_turns():
    llm = _ScriptedLLM([
        LLMResponse(content="第一轮答复"),
        LLMResponse(content="第二轮答复"),
    ])
    agent = TaskAgent(llm, ToolRegistry())
    session = _session()
    messages = agent.new_conversation()

    reply1, _ = agent.converse(session, messages, "第一句")
    reply2, _ = agent.converse(session, messages, "第二句")

    assert reply1 == "第一轮答复"
    assert reply2 == "第二轮答复"
    # 第二轮 LLM 看到的 messages 应包含第一轮的用户+助手消息（上下文连续）。
    second_turn_msgs = llm.seen_messages[-1]
    texts = [m.content for m in second_turn_msgs]
    assert "第一句" in texts
    assert "第一轮答复" in texts
    assert "第二句" in texts


# --- ChatController ----------------------------------------------------------

def test_chat_controller_send_and_persist():
    ws = _session().workspace
    repo = _repo(ws)
    session = AgentSession(session_id="w1", workspace=ws, task=WritingTask(""))
    llm = _ScriptedLLM([
        LLMResponse(content="", tool_calls=[
            ToolCall(id="c1", name="rewrite_section",
                     arguments={"section_id": "intro", "new_content": "崭新引言"})]),
        LLMResponse(content="已改写引言"),
    ])
    registry = ToolRegistry()
    from paper_agent.agent_platform.tools.context import ToolContext
    from paper_agent.agent_platform.tools.edit import register_rewrite_section
    ctx = ToolContext(session=session, repo=repo, gate=GuardrailGate(), elicitor=None)
    register_rewrite_section(registry, ctx)
    agent = TaskAgent(llm, registry)

    controller = ChatController(agent, session, repo)
    turn = controller.send("改写引言")
    assert turn.reply == "已改写引言"
    assert "rewrite_section" in turn.tool_calls
    assert repo.load("w1").section_drafts["intro"].content == "崭新引言"
    # 会话已持久化。
    from paper_agent.agent_platform.session_store import load_session
    assert load_session(repo, "w1") is not None


# --- REPL --------------------------------------------------------------------

def test_streaming_chat_sink_writes_only_content_deltas():
    from paper_agent.agent_platform.chat import StreamingChatSink
    from paper_agent.observability.events import Event, EventKind

    written = []
    sink = StreamingChatSink(write=written.append)
    sink.emit(Event(kind=EventKind.LLM_DELTA, message="正文", data={"kind": "content"}))
    sink.emit(Event(kind=EventKind.LLM_DELTA, message="思考", data={"kind": "thinking"}))
    sink.emit(Event(kind=EventKind.LLM_REQUEST, message="prompt 预览"))
    # 只写内容增量，思考与请求预览都忽略。
    assert written == ["正文"]


def test_repl_streaming_mode_does_not_reprint_reply():
    ws = _session().workspace
    repo = _repo(ws)
    session = AgentSession(session_id="w1", workspace=ws, task=WritingTask(""))
    agent = TaskAgent(_ScriptedLLM([LLMResponse(content="完整答复文本")]), ToolRegistry())
    controller = ChatController(agent, session, repo)

    outputs = []
    inputs = iter(["/exit"])
    run_chat_repl(
        controller,
        initial_message="问题",
        input_fn=lambda p: next(inputs),
        output_fn=outputs.append,
        streaming=True,
    )
    joined = "".join(outputs)
    # 流式模式下 REPL 打印前缀但不整段重复答复正文（正文应由 sink 流式输出）。
    assert "助手 > " in joined
    assert "完整答复文本" not in joined


def test_handle_turn_auto_continues_on_max_iters_with_progress():
    from paper_agent.agent_platform.chat import ChatTurn, _handle_turn

    class _FakeCtrl:
        def __init__(self, turns):
            self._turns = list(turns)
            self.sent = []

        def send(self, text):
            self.sent.append(text)
            return self._turns.pop(0)

    # 第一轮撞上限但有进展 → 自动续跑；第二轮自然完成。
    ctrl = _FakeCtrl([
        ChatTurn(reply="做了一半", bound_hit="max_iters", made_progress=True),
        ChatTurn(reply="全部完成", bound_hit=None, made_progress=True),
    ])
    outputs = []
    _handle_turn(ctrl, "润色全文", outputs.append, streaming=False)
    # 自动续跑了一次（共发送 2 次）。
    assert len(ctrl.sent) == 2
    assert any("自动继续" in o for o in outputs)


def test_handle_turn_stops_auto_continue_when_stagnant():
    from paper_agent.agent_platform.chat import ChatTurn, _handle_turn

    class _FakeCtrl:
        def __init__(self, turn):
            self._turn = turn
            self.sent = []

        def send(self, text):
            self.sent.append(text)
            return self._turn

    # 撞上限但无进展 → 不自动续跑，提示卡住。
    ctrl = _FakeCtrl(ChatTurn(reply="卡住", bound_hit="max_iters", made_progress=False))
    outputs = []
    _handle_turn(ctrl, "改", outputs.append, streaming=False)
    assert len(ctrl.sent) == 1  # 只发了一次，没有自动续跑
    assert any("卡住" in o for o in outputs)


def test_repl_exit_command():
    ws = _session().workspace
    repo = _repo(ws)
    session = AgentSession(session_id="w1", workspace=ws, task=WritingTask(""))
    agent = TaskAgent(_ScriptedLLM([]), ToolRegistry())
    controller = ChatController(agent, session, repo)

    outputs = []
    inputs = iter(["/exit"])
    run_chat_repl(
        controller,
        input_fn=lambda prompt: next(inputs),
        output_fn=outputs.append,
    )
    assert any("再见" in o for o in outputs)


def test_repl_initial_message_and_one_turn():
    ws = _session().workspace
    repo = _repo(ws)
    session = AgentSession(session_id="w1", workspace=ws, task=WritingTask(""))
    agent = TaskAgent(_ScriptedLLM([LLMResponse(content="收到你的问题")]), ToolRegistry())
    controller = ChatController(agent, session, repo)

    outputs = []
    inputs = iter(["/exit"])
    run_chat_repl(
        controller,
        initial_message="帮我看看引言",
        input_fn=lambda prompt: next(inputs),
        output_fn=outputs.append,
    )
    assert any("收到你的问题" in o for o in outputs)


# --- open_chat 集成（无初稿/主题也能开对话） --------------------------------

def test_open_chat_allows_empty_initial_task():
    repo = WorkspaceRepository(_MemStore())
    retrieval = _FakeRetrieval()
    app = PaperAgentApp(
        llm=_ScriptedLLM([LLMResponse(content="你好，我能帮你写论文")]),
        repo=repo,
        gate=GuardrailGate(),
        retrieval=retrieval,
        verifier=CitationVerifier(retrieval),
        pipeline_runner=lambda wid: None,
    )
    controller = app.open_chat(WritingTask(instruction=""))
    turn = controller.send("你好")
    assert turn.reply == "你好，我能帮你写论文"


class _FakeRetrieval:
    def search(self, query, limit=5):
        return []

    def fetch_metadata(self, source_id):
        return None
