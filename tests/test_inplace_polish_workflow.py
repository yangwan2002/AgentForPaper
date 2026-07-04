"""InplacePolishWorkflow 测试：按扩展名分派、产物为新文件、原稿无损、失败诚实上报。

.tex 用返回空润色的 fake LLM（守卫拦截全部 → 产物逐字节等于原文，证明保结构不破坏）；
.docx 用 polish_language=False（保结构复制，不依赖 LLM），python-docx 缺失时跳过。
"""

from __future__ import annotations

import copy

import pytest

from paper_agent.agent_platform.guardrail_gate import GuardrailGate
from paper_agent.agent_platform.models import AgentSession, WritingTask
from paper_agent.agent_platform.routing import Intent
from paper_agent.agent_platform.tools.context import ToolContext
from paper_agent.agent_platform.workflows import InplacePolishWorkflow
from paper_agent.agent_platform.workflows.base import WorkflowResult
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
    """返回空内容 → 守卫拦截 → 保留原文（保结构不破坏）。"""

    def complete(self, messages, **opts):
        return LLMResponse(content="")


def _ctx(tmp_path, ws=None):
    ws = ws or PaperWorkspace(workspace_id="w1", input_mode=InputMode.DRAFT_REVISION)
    repo = WorkspaceRepository(_MemStore())
    repo.create(ws)
    session = AgentSession(session_id="w1", workspace=ws, task=WritingTask("t"))
    return ToolContext(
        session=session, repo=repo, gate=GuardrailGate(),
        elicitor=AutoElicitor(), output_dir=str(tmp_path),
    )


_TEX = r"""\documentclass{article}
\usepackage{amsmath}
\newcommand{\mycmd}[1]{\textbf{#1}}
\begin{document}
\section{Introduction}
This is a substantial prose paragraph that should be considered for polishing.
It cites prior work \cite{smith2020} and references equation \ref{eq:main}.
\begin{equation}\label{eq:main}
E = mc^2
\end{equation}
\end{document}
"""


def test_intent_binding():
    assert InplacePolishWorkflow(_NoopLLM()).intent is Intent.INPLACE_POLISH


def test_missing_source_reports_unresolved(tmp_path):
    ctx = _ctx(tmp_path)
    result = InplacePolishWorkflow(_NoopLLM()).run(ctx, {})
    assert isinstance(result, WorkflowResult)
    assert result.ok is False
    assert result.unresolved and "未找到" in result.unresolved[0]


def test_unsupported_ext_reports_unresolved(tmp_path):
    src = tmp_path / "note.md"
    src.write_text("# hi", encoding="utf-8")
    ctx = _ctx(tmp_path)
    result = InplacePolishWorkflow(_NoopLLM()).run(ctx, {"source_path": str(src)})
    assert result.ok is False
    assert any("仅支持" in u for u in result.unresolved)


def test_latex_produces_new_file_and_keeps_source_bytes(tmp_path):
    """.tex 分派到 latex 处理器；产物为新文件、原稿字节不变。"""
    src = tmp_path / "paper.tex"
    src.write_text(_TEX, encoding="utf-8")
    original = src.read_bytes()
    ctx = _ctx(tmp_path)

    result = InplacePolishWorkflow(_NoopLLM()).run(ctx, {"source_path": str(src)})
    assert result.ok is True
    produced = tmp_path / "paper_inplace.tex"
    assert produced.exists()
    assert str(produced) in result.files
    # 原稿逐字节不变。
    assert src.read_bytes() == original


def test_docx_dispatch_copy_when_no_polish(tmp_path):
    """.docx 分派到 docx 处理器；polish_language=False 保结构复制，产物为新文件。"""
    docx = pytest.importorskip("docx")
    src = tmp_path / "paper.docx"
    document = docx.Document()
    document.add_paragraph("Some prose paragraph.")
    document.save(str(src))
    original = src.read_bytes()
    ctx = _ctx(tmp_path)

    result = InplacePolishWorkflow(_NoopLLM()).run(
        ctx, {"source_path": str(src), "polish_language": False}
    )
    assert result.ok is True
    produced = tmp_path / "paper_inplace.docx"
    assert produced.exists()
    assert str(produced) in result.files
    assert src.read_bytes() == original
