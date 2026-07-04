"""任务收尾验收接入 TaskAgent 的集成测试（Task 4）。

用可编排的 fake LLM 驱动顶层循环，验证：
- 解析可测约束（格式/数量/年限）；
- 收尾前跑确定性验收，把已满足/未满足项写入 TaskResult；
- 无可测约束时收尾器为 no-op（Property 9 向后兼容）；
- 可自愈项经 Top_Agent 修正重验后通过；不可解决项有界后诚实上报。
"""

from __future__ import annotations

import copy

from paper_agent.agent_platform.finalize import (
    make_acceptance_finalizer,
    parse_requirements,
)
from paper_agent.agent_platform.models import AgentSession, WritingTask
from paper_agent.agent_platform.task_agent import TaskAgent
from paper_agent.providers.llm.base import LLMResponse, ToolCall
from paper_agent.tools.registry import ToolRegistry
from paper_agent.workspace.models import (
    InputMode,
    OutlineNode,
    OutputFormat,
    PaperWorkspace,
    ReferenceEntry,
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
    def __init__(self, script=None):
        self._script = list(script or [])
        self._i = 0

    def complete(self, messages, **opts):
        if self._i < len(self._script):
            resp = self._script[self._i]
            self._i += 1
            return resp
        return LLMResponse(content="收尾。")


def _ws(instruction_format=OutputFormat.MARKDOWN, dangling=False, refs=1) -> PaperWorkspace:
    ws = PaperWorkspace(
        workspace_id="w1", input_mode=InputMode.DRAFT_REVISION,
        output_format=instruction_format,
    )
    ws.outline = [OutlineNode(section_id="s1", title="Intro", order=0)]
    ws.verified_references = [
        ReferenceEntry(id=str(i + 1), title=f"T{i}", authors=["A"], year=2021,
                       source_id=f"d{i}", verified=True)
        for i in range(refs)
    ]
    content = "正常中文正文，引用 [1]。"
    if dangling:
        content = "正常中文正文，引用 [1] 和 [99]。"
    ws.section_drafts = {"s1": SectionDraft(section_id="s1", title="Intro", content=content)}
    return ws


def _session(ws, instruction) -> AgentSession:
    return AgentSession(session_id="w1", workspace=ws, task=WritingTask(instruction))


# --------------------------------------------------------------------------- #
# 需求解析
# --------------------------------------------------------------------------- #

def test_parse_requirements_detects_format_and_bounds():
    ws = _ws()
    req = parse_requirements("帮我导出为 docx，至少 5 篇参考文献，用近 3 年的文献", ws)
    assert req.expected_format == "docx"
    assert req.reference_count_min == 5
    assert req.min_year is not None
    assert req.require_citation_closure is True


def test_parse_requirements_empty_when_no_measurable():
    ws = _ws()
    req = parse_requirements("帮我看看这段写得怎么样", ws)
    assert req.has_any() is False


# --------------------------------------------------------------------------- #
# 收尾验收接入
# --------------------------------------------------------------------------- #

def test_finalizer_noop_when_no_requirements(tmp_path):
    ws = _ws()
    agent = TaskAgent(
        _ScriptedLLM([LLMResponse(content="随便聊聊。")]),
        ToolRegistry(),
        acceptance_finalizer=make_acceptance_finalizer(str(tmp_path)),
    )
    result = agent.run(_session(ws, "随便聊聊"))
    # 无可测约束 → 收尾器 no-op，completed/unfinished 保持空。
    assert result.completed == []
    assert result.unfinished == []


def test_finalizer_clean_paper_delivers(tmp_path):
    ws = _ws()
    agent = TaskAgent(
        _ScriptedLLM([LLMResponse(content="已导出。")]),
        ToolRegistry(),
        acceptance_finalizer=make_acceptance_finalizer(str(tmp_path)),
    )
    result = agent.run(_session(ws, "帮我导出为 markdown"))
    assert result.guardrail_report.get("acceptance_passed") is True
    assert result.unfinished == []
    assert any("citation_closure" in c for c in result.completed)
    # 产出文件被记入结果。
    assert any(f.endswith(".md") for f in result.export_files)


def test_finalizer_reports_unmet_quantity_bounded(tmp_path):
    ws = _ws(refs=1)
    # LLM 的自愈轮什么也做不成（只回收尾文本，不调工具）。
    agent = TaskAgent(
        _ScriptedLLM(),
        ToolRegistry(),
        acceptance_finalizer=make_acceptance_finalizer(str(tmp_path), max_heal_rounds=2),
    )
    result = agent.run(_session(ws, "导出为 markdown，至少 5 篇参考文献"))
    assert result.guardrail_report.get("acceptance_passed") is False
    assert result.guardrail_report.get("acceptance_heal_rounds") == 2  # 有界
    assert any("quantity" in u for u in result.unfinished)


def test_finalizer_self_heals_dangling_citation(tmp_path):
    """悬空引用 → 收尾验收发现 → 自愈轮调 rewrite_section 修正 → 重验通过。"""
    from paper_agent.agent_platform.guardrail_gate import GuardrailGate
    from paper_agent.agent_platform.tools.context import ToolContext
    from paper_agent.agent_platform.tools.edit import register_rewrite_section
    from paper_agent.elicitation import AutoElicitor

    ws = _ws(dangling=True)
    repo = WorkspaceRepository(_MemStore())
    repo.create(ws)
    session = _session(ws, "导出为 markdown")

    ctx = ToolContext(
        session=session, repo=repo, gate=GuardrailGate(),
        elicitor=AutoElicitor(), output_dir=str(tmp_path),
    )
    registry = ToolRegistry()
    register_rewrite_section(registry, ctx)

    # 主循环先自然收尾（不修，留下悬空引用）；随后收尾验收发现悬空 → 自愈轮
    # 调 rewrite_section 去掉悬空的 [99] 再收尾。
    fixed = "正常中文正文，引用 [1]。"
    script = [
        LLMResponse(content="已导出。"),  # 主循环收尾，dangling 仍在
        LLMResponse(content="", tool_calls=[ToolCall(
            id="h1", name="rewrite_section",
            arguments={"section_id": "s1", "new_content": fixed},
        )]),
        LLMResponse(content="已修正悬空引用。"),
    ]
    agent = TaskAgent(
        _ScriptedLLM(script), registry,
        acceptance_finalizer=make_acceptance_finalizer(str(tmp_path), max_heal_rounds=2),
    )
    result = agent.run(session)
    assert result.guardrail_report.get("acceptance_passed") is True
    assert ws.section_drafts["s1"].content == fixed
