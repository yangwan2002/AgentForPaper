"""intent-routing-and-workflows 属性测试（Property 1-9）。

覆盖：信号路由可复现 + 冲突降置信、意图封闭、低置信必问、固定任务不经模型编排、
原稿无损、失败诚实、写入经护栏（不动工作区）、向后兼容、回显可拦截误判。

真实 pandoc 相关的原稿无损用「保结构 latex 润色」验证（无需 pandoc）；转格式核心
的失败诚实分支不依赖 pandoc。
"""

from __future__ import annotations

import copy
import json

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

_ALLOW_TMP = [HealthCheck.function_scoped_fixture]

from paper_agent.agent_platform.guardrail_gate import GuardrailGate
from paper_agent.agent_platform.models import AgentSession, WritingTask
from paper_agent.agent_platform.routing import (
    Intent,
    IntentRouter,
    RouteDecision,
    _normalize_intent,
    confirm_intent,
    detect_signals,
)
from paper_agent.agent_platform.tools.context import ToolContext
from paper_agent.agent_platform.workflows import ConvertWorkflow, InplacePolishWorkflow
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


class _FixedLLM:
    """返回固定分类 JSON 的 LLM（供 IntentRouter；确定性）。"""

    def __init__(self, intent: str, confidence: float = 0.9):
        self._payload = json.dumps(
            {"intent": intent, "confidence": confidence, "rephrase": "r"}
        )

    def complete(self, messages, **opts):
        return LLMResponse(content=self._payload)


class _GarbageLLM:
    """返回任意（可能非法）内容的 LLM——用于意图封闭性。"""

    def __init__(self, content: str):
        self._content = content

    def complete(self, messages, **opts):
        return LLMResponse(content=self._content)


class _NoopLLM:
    """空润色 → 守卫拦截 → 保留原文（保结构不破坏）。"""

    def complete(self, messages, **opts):
        return LLMResponse(content="")


def _ws(**profile):
    ws = PaperWorkspace(workspace_id="w1", input_mode=InputMode.DRAFT_REVISION)
    ws.profile.update(profile)
    return ws


def _ctx(tmp_path, ws=None, elicitor=None):
    ws = ws or _ws()
    repo = WorkspaceRepository(_MemStore())
    repo.create(ws)
    session = AgentSession(session_id="w1", workspace=ws, task=WritingTask("t"))
    return ToolContext(
        session=session, repo=repo, gate=GuardrailGate(),
        elicitor=elicitor or ScriptedElicitor({}), output_dir=str(tmp_path),
    )


# --------------------------------------------------------------------------- #
# Property 1: 确定性信号路由可复现（+ 信号与 LLM 一致/冲突的置信调整确定）
# --------------------------------------------------------------------------- #
@settings(max_examples=60)
@given(
    verb=st.sampled_from(["转成", "转为", "导出为", "转换成"]),
    fmt=st.sampled_from(["docx", "word", "latex", "markdown"]),
)
def test_prop1_signal_detection_reproducible(verb, fmt):
    ws = _ws(source_document_path="C:/x/paper.tex", source_document_ext=".tex")
    text = f"帮我把这篇{verb}{fmt}"
    a = detect_signals(text, ws)
    b = detect_signals(text, ws)
    assert a == b  # 纯函数、可复现
    intents, _params, _ = a
    # 目标格式与源（.tex→latex）相同为空操作、不算转换；跨格式才命中转格式。
    if fmt == "latex":
        assert Intent.CONVERT_FORMAT not in intents
    else:
        assert Intent.CONVERT_FORMAT in intents


# --------------------------------------------------------------------------- #
# Property 2: 意图取值封闭（任意 LLM 输出都归一到枚举，绝不产生枚举外意图）
# --------------------------------------------------------------------------- #
@settings(max_examples=100)
@given(raw=st.text(max_size=40))
def test_prop2_intent_closed_under_normalization(raw):
    assert _normalize_intent(raw) in set(Intent)


@settings(max_examples=80)
@given(content=st.text(max_size=60))
def test_prop2_route_intent_always_enum(content):
    router = IntentRouter(_GarbageLLM(content))
    decision = router.route("随便一句话", _ws())
    assert isinstance(decision, RouteDecision)
    assert decision.intent in set(Intent)


# --------------------------------------------------------------------------- #
# Property 3: 低置信必问（固定任务低于阈值 → needs_confirmation）
# --------------------------------------------------------------------------- #
@settings(max_examples=50)
@given(conf=st.floats(min_value=0.0, max_value=0.5))
def test_prop3_low_confidence_requires_confirmation(conf):
    # always_confirm_fixed=False，使确认完全由置信度决定。
    router = IntentRouter(
        _FixedLLM("convert_format", confidence=conf),
        confidence_threshold=0.75,
        always_confirm_fixed=False,
    )
    decision = router.route("处理一下这个文件", _ws())
    if decision.intent in Intent.fixed_tasks():
        assert decision.needs_confirmation is True


# --------------------------------------------------------------------------- #
# Property 4: 固定任务不经模型编排（工具序列由工作流代码决定，不调顶层 LLM）
# --------------------------------------------------------------------------- #
def test_prop4_convert_workflow_no_llm_in_sequence(tmp_path, monkeypatch):
    """ConvertWorkflow 固定调用 convert_document_core，不经任何 LLM 决定序列。"""
    calls = {"n": 0}

    from paper_agent.agent_platform.workflows import convert_workflow as cw
    from paper_agent.agent_platform.tools.convert_tool import ConvertOutcome

    def _fake_core(ctx, **kwargs):
        calls["n"] += 1
        return ConvertOutcome(ok=True, files=["output/x.docx"], notes=["ok"])

    monkeypatch.setattr(cw, "convert_document_core", _fake_core)
    ctx = _ctx(tmp_path)
    result = ConvertWorkflow().run(ctx, {"to_format": "docx", "source_path": "a.tex"})
    assert result.ok is True
    assert calls["n"] == 1  # 恰好一次固定调用，序列由代码写死


# --------------------------------------------------------------------------- #
# Property 5: 原稿无损（工作流执行后用户原始输入文件字节不变）
# --------------------------------------------------------------------------- #
_TEX = (
    "\\documentclass{article}\n\\begin{document}\n\\section{Intro}\n"
    "This is a prose paragraph that could be polished. It cites \\cite{a}.\n"
    "\\end{document}\n"
)


@settings(max_examples=25, deadline=None, suppress_health_check=_ALLOW_TMP)
@given(extra=st.text(alphabet="abc DEF.,", min_size=0, max_size=40))
def test_prop5_source_bytes_unchanged(extra, tmp_path):
    src = tmp_path / "paper.tex"
    body = _TEX.replace("Intro", "Intro " + extra) if extra.strip() else _TEX
    src.write_text(body, encoding="utf-8")
    original = src.read_bytes()

    ctx = _ctx(tmp_path)
    result = InplacePolishWorkflow(_NoopLLM()).run(ctx, {"source_path": str(src)})
    assert isinstance(result, WorkflowResult)
    # 无论成功与否，原稿逐字节不变。
    assert src.read_bytes() == original
    # 产物（若有）是新文件，不等于原稿路径。
    for f in result.files:
        assert f != str(src)


# --------------------------------------------------------------------------- #
# Property 6: 失败诚实（缺源 → ok=False 且 unresolved 非空；不产降级产物）
# --------------------------------------------------------------------------- #
@settings(max_examples=30, suppress_health_check=_ALLOW_TMP)
@given(to_format=st.sampled_from(["docx", "latex", "markdown", "pdf"]))
def test_prop6_failure_is_honest(to_format, tmp_path):
    ctx = _ctx(tmp_path)  # 无源文件
    result = ConvertWorkflow().run(ctx, {"to_format": to_format})
    assert result.ok is False
    assert result.unresolved
    assert result.files == []


# --------------------------------------------------------------------------- #
# Property 7: 写入经护栏（工作流不改工作区 section_drafts，只产新文件）
# --------------------------------------------------------------------------- #
def test_prop7_workflow_does_not_mutate_workspace(tmp_path):
    src = tmp_path / "paper.tex"
    src.write_text(_TEX, encoding="utf-8")
    ctx = _ctx(tmp_path)
    before = copy.deepcopy(ctx.workspace.section_drafts)
    InplacePolishWorkflow(_NoopLLM()).run(ctx, {"source_path": str(src)})
    # 保结构工作流只读工作区、产出独立文件，不动 section_drafts。
    assert ctx.workspace.section_drafts == before


# --------------------------------------------------------------------------- #
# Property 8: 向后兼容（confirm_intent 对开放意图/免确认直接放行、不问）
# --------------------------------------------------------------------------- #
@settings(max_examples=40)
@given(intent=st.sampled_from(list(Intent)))
def test_prop8_open_or_no_confirm_proceeds_without_asking(intent):
    # needs_confirmation=False → 直接 proceed，不触发任何交互。
    decision = RouteDecision(intent=intent, confidence=1.0, needs_confirmation=False)
    outcome = confirm_intent(decision, ScriptedElicitor({}))
    assert outcome.proceed is True
    assert outcome.intent is intent


# --------------------------------------------------------------------------- #
# Property 9: 回显可拦截误判（用户取消 → 不 proceed）
# --------------------------------------------------------------------------- #
@settings(max_examples=40)
@given(conf=st.floats(min_value=0.75, max_value=1.0))
def test_prop9_echo_cancel_blocks_execution(conf):
    decision = RouteDecision(
        intent=Intent.CONVERT_FORMAT, confidence=conf,
        needs_confirmation=True, candidates=[Intent.CONVERT_FORMAT],
        rephrase="把文稿转成 docx",
    )
    elicitor = ScriptedElicitor({"intent_echo": "取消"})
    outcome = confirm_intent(decision, elicitor, threshold=0.75)
    assert outcome.proceed is False
