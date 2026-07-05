"""InplacePolishWorkflow + 只读审计接入测试（inplace-polish-audit · Task 3）。

覆盖：注入 auditor → notes 含审计报告且 files/ok 不变；auditor=None → 与现状一致；
审计抛异常 → 不连累润色产物。
"""

from __future__ import annotations

import copy

from paper_agent.agent_platform.audit import AuditReport
from paper_agent.agent_platform.guardrail_gate import GuardrailGate
from paper_agent.agent_platform.models import AgentSession, WritingTask
from paper_agent.agent_platform.tools.context import ToolContext
from paper_agent.agent_platform.workflows import InplacePolishWorkflow
from paper_agent.elicitation import AutoElicitor
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


class _NoopLLM:
    def complete(self, messages, **opts):
        return LLMResponse(content="")


def _ctx(tmp_path):
    ws = PaperWorkspace(workspace_id="w1", input_mode=InputMode.DRAFT_REVISION)
    repo = WorkspaceRepository(_MemStore())
    repo.create(ws)
    session = AgentSession(session_id="w1", workspace=ws, task=WritingTask("t"))
    return ToolContext(
        session=session, repo=repo, gate=GuardrailGate(),
        elicitor=AutoElicitor(), output_dir=str(tmp_path),
    )


_TEX = "\\documentclass{article}\n\\begin{document}\n\\section{Intro}\nProse.\n\\end{document}\n"


class _StubAuditor:
    def __init__(self, report):
        self._report = report
        self.calls = []

    def audit(self, src):
        self.calls.append(src)
        return self._report


class _BoomAuditor:
    def audit(self, src):
        raise RuntimeError("audit boom")


def _src(tmp_path):
    p = tmp_path / "paper.tex"
    p.write_text(_TEX, encoding="utf-8")
    return p


def test_audit_report_appended_to_notes(tmp_path):
    src = _src(tmp_path)
    report = AuditReport(ran=True, reference_total=1, reference_real=0,
                         reference_unverifiable=1)
    auditor = _StubAuditor(report)
    ctx = _ctx(tmp_path)
    result = InplacePolishWorkflow(_NoopLLM(), auditor=auditor).run(
        ctx, {"source_path": str(src)}
    )
    assert result.ok is True
    assert result.files
    assert auditor.calls == [str(src)]
    joined = "".join(result.notes)
    assert "文献与引用审计" in joined
    assert "未核验" in joined


def test_none_auditor_backward_compatible(tmp_path):
    src = _src(tmp_path)
    ctx = _ctx(tmp_path)
    result = InplacePolishWorkflow(_NoopLLM(), auditor=None).run(
        ctx, {"source_path": str(src)}
    )
    assert result.ok is True
    assert result.files
    # 无审计：notes 只含润色处理器输出，不含审计块。
    assert not any("文献与引用审计" in n for n in result.notes)


def test_audit_exception_does_not_break_polish(tmp_path):
    src = _src(tmp_path)
    ctx = _ctx(tmp_path)
    result = InplacePolishWorkflow(_NoopLLM(), auditor=_BoomAuditor()).run(
        ctx, {"source_path": str(src)}
    )
    # 审计抛异常 → 润色产物仍在、ok 不翻转，notes 记录未完成。
    assert result.ok is True
    assert result.files
    assert any("审计未完成" in n for n in result.notes)
