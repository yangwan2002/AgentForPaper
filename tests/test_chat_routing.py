"""ChatController 意图路由分流集成测试（intent-routing-and-workflows · Task 5）。

用 Mock LLM（分类器）+ ScriptedElicitor（确认交互）+ 假工作流验证分流：
- 转格式（高置信 fixed）→ 回显确认后走对应 Workflow、不进 TaskAgent；
- 回显否定 → 不执行工作流、也不进 TaskAgent；
- 用户改选「按开放处理」→ 回落既有 TaskAgent；
- 开放意图 → 直接走既有 TaskAgent（行为不变）；
- routing_enabled=False → 全部走既有 TaskAgent（向后兼容 Property 8）。
"""

from __future__ import annotations

import copy
import json

from paper_agent.agent_platform.chat import ChatController
from paper_agent.agent_platform.guardrail_gate import GuardrailGate
from paper_agent.agent_platform.models import AgentSession, WritingTask
from paper_agent.agent_platform.routing import Intent, IntentRouter
from paper_agent.agent_platform.task_agent import TaskAgent
from paper_agent.agent_platform.tools.context import ToolContext
from paper_agent.agent_platform.workflows.base import WorkflowResult
from paper_agent.elicitation import ScriptedElicitor
from paper_agent.providers.llm.base import LLMResponse
from paper_agent.workspace.models import InputMode, PaperWorkspace
from paper_agent.workspace.repository import WorkspaceRepository


class _MemStore:
    def __init__(self):
        self._data = {}

    def load(self, wid):
        raw = self._data.get(wid)
        return PaperWorkspace.from_dict(raw) if raw else None

    def save(self, ws):
        self._data[ws.workspace_id] = copy.deepcopy(ws.to_dict())


class _ClassifierLLM:
    """按预置意图返回分类 JSON（作为 IntentRouter 的 LLM）。"""

    def __init__(self, intent: str, confidence: float = 0.95):
        self._payload = json.dumps(
            {"intent": intent, "confidence": confidence, "rephrase": "复述"}
        )

    def complete(self, messages, **opts):
        return LLMResponse(content=self._payload)


class _SpyAgentLLM:
    """记录是否被调用——用于断言"是否进了 TaskAgent"。"""

    def __init__(self):
        self.called = False

    def complete(self, messages, **opts):
        self.called = True
        return LLMResponse(content="来自自由智能体的答复。")


class _SpyWorkflow:
    """假工作流：记录是否被执行，返回固定成功结果。"""

    def __init__(self, intent: Intent):
        self.intent = intent
        self.ran = False

    def run(self, ctx, params) -> WorkflowResult:
        self.ran = True
        return WorkflowResult(ok=True, files=["output/x.docx"], notes=["已转换。"])


def _build(tmp_path, *, classify_intent, elicitor, routing_enabled=True):
    ws = PaperWorkspace(workspace_id="w1", input_mode=InputMode.DRAFT_REVISION)
    repo = WorkspaceRepository(_MemStore())
    repo.create(ws)
    session = AgentSession(session_id="w1", workspace=ws, task=WritingTask(""))
    ctx = ToolContext(
        session=session, repo=repo, gate=GuardrailGate(),
        elicitor=elicitor, output_dir=str(tmp_path),
    )
    agent_llm = _SpyAgentLLM()
    from paper_agent.tools.registry import ToolRegistry

    agent = TaskAgent(agent_llm, ToolRegistry())
    router = IntentRouter(_ClassifierLLM(classify_intent))
    wf = _SpyWorkflow(Intent.CONVERT_FORMAT)
    controller = ChatController(
        agent, session, repo, output_dir=str(tmp_path),
        enable_acceptance=False,
        router=router,
        workflows={Intent.CONVERT_FORMAT: wf},
        tool_context=ctx,
        routing_enabled=routing_enabled,
    )
    return controller, agent_llm, wf


def test_convert_goes_through_workflow_not_agent(tmp_path):
    """转格式高置信 → 回显确认「开始」→ 走工作流、不进 TaskAgent。"""
    elicitor = ScriptedElicitor({"intent_echo": "开始"})
    controller, agent_llm, wf = _build(
        tmp_path, classify_intent="convert_format", elicitor=elicitor
    )
    turn = controller.send("把这篇 tex 转成 docx")
    assert wf.ran is True
    assert agent_llm.called is False  # 没进自由智能体
    assert "已转换。" in turn.reply
    assert turn.tool_calls == ["convert_format"]


def test_echo_reject_does_not_execute(tmp_path):
    """回显否定（取消）→ 工作流不执行、也不进 TaskAgent（问前不动手）。"""
    elicitor = ScriptedElicitor({"intent_echo": "取消"})
    controller, agent_llm, wf = _build(
        tmp_path, classify_intent="convert_format", elicitor=elicitor
    )
    turn = controller.send("把这篇 tex 转成 docx")
    assert wf.ran is False
    assert agent_llm.called is False
    assert "取消" in turn.reply


def test_switch_to_open_falls_back_to_agent(tmp_path):
    """回显选「换个任务（按开放处理）」→ 不执行工作流，回落既有 TaskAgent。"""
    elicitor = ScriptedElicitor({"intent_echo": "换个任务（按开放处理）"})
    controller, agent_llm, wf = _build(
        tmp_path, classify_intent="convert_format", elicitor=elicitor
    )
    turn = controller.send("把这篇 tex 转成 docx")
    assert wf.ran is False
    assert agent_llm.called is True
    assert "自由智能体" in turn.reply


def test_open_intent_goes_to_agent(tmp_path):
    """开放意图 → 直接走既有 TaskAgent（不触发确认/工作流）。"""
    elicitor = ScriptedElicitor({})
    controller, agent_llm, wf = _build(
        tmp_path, classify_intent="open", elicitor=elicitor
    )
    turn = controller.send("帮我写一段引言")
    assert wf.ran is False
    assert agent_llm.called is True


def test_routing_disabled_is_backward_compatible(tmp_path):
    """routing_enabled=False → 即便命中固定任务也全部走既有 TaskAgent。"""
    elicitor = ScriptedElicitor({"intent_echo": "开始"})
    controller, agent_llm, wf = _build(
        tmp_path, classify_intent="convert_format", elicitor=elicitor,
        routing_enabled=False,
    )
    turn = controller.send("把这篇 tex 转成 docx")
    assert wf.ran is False
    assert agent_llm.called is True


class _FollowupWorkflow:
    """假工作流：转换成功且带核心未覆盖的 followups（触发兜底转交）。"""

    def __init__(self, intent: Intent):
        self.intent = intent
        self.ran = False

    def run(self, ctx, params) -> WorkflowResult:
        self.ran = True
        return WorkflowResult(
            ok=True, files=["output/x.docx"], notes=["已转换。"],
            followups=["让图跨双栏放置（不要挤在单栏）", "设置正文字体/字号"],
        )


def _build_with_workflow(tmp_path, wf, *, classify_intent, elicitor):
    ws = PaperWorkspace(workspace_id="w1", input_mode=InputMode.DRAFT_REVISION)
    repo = WorkspaceRepository(_MemStore())
    repo.create(ws)
    session = AgentSession(session_id="w1", workspace=ws, task=WritingTask(""))
    ctx = ToolContext(
        session=session, repo=repo, gate=GuardrailGate(),
        elicitor=elicitor, output_dir=str(tmp_path),
    )
    agent_llm = _SpyAgentLLM()
    from paper_agent.tools.registry import ToolRegistry

    agent = TaskAgent(agent_llm, ToolRegistry())
    router = IntentRouter(_ClassifierLLM(classify_intent))
    controller = ChatController(
        agent, session, repo, output_dir=str(tmp_path),
        enable_acceptance=False, router=router,
        workflows={wf.intent: wf}, tool_context=ctx, routing_enabled=True,
    )
    return controller, agent_llm


def test_uncovered_followups_spill_to_agent(tmp_path):
    """转换成功但有核心未覆盖的排版细项 → 兜底转交自由智能体（不静默丢弃）。"""
    elicitor = ScriptedElicitor({"intent_echo": "开始"})
    wf = _FollowupWorkflow(Intent.CONVERT_FORMAT)
    controller, agent_llm = _build_with_workflow(
        tmp_path, wf, classify_intent="convert_format", elicitor=elicitor
    )
    turn = controller.send('"D:\\p\\paper.tex" 转 docx，双栏，五号，图双栏放置')
    assert wf.ran is True
    assert agent_llm.called is True                    # 兜底转交进了自由智能体
    assert "已转换。" in turn.reply                     # 保留确定性核心的结论
    assert "来自自由智能体的答复。" in turn.reply        # 又拼上了转交处理的回复


def test_no_followups_no_spill(tmp_path):
    """转换成功且无 followups → 不转交、不进自由智能体（行为不变）。"""
    elicitor = ScriptedElicitor({"intent_echo": "开始"})
    controller, agent_llm, wf = _build(
        tmp_path, classify_intent="convert_format", elicitor=elicitor
    )
    turn = controller.send("把这篇 tex 转成 docx")
    assert wf.ran is True
    assert agent_llm.called is False
