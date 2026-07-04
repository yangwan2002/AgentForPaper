"""ConvertWorkflow 测试：固定步骤、产物为新文件、原稿无损、失败诚实上报。

真实 pandoc 用例在无 pandoc 时跳过；错误分支不依赖 pandoc。
"""

from __future__ import annotations

import copy

import pytest

from paper_agent.agent_platform.guardrail_gate import GuardrailGate
from paper_agent.agent_platform.models import AgentSession, WritingTask
from paper_agent.agent_platform.routing import Intent
from paper_agent.agent_platform.tools.context import ToolContext
from paper_agent.agent_platform.workflows import ConvertWorkflow
from paper_agent.agent_platform.workflows.base import WorkflowResult
from paper_agent.elicitation import AutoElicitor
from paper_agent.export.pandoc_pipeline import PandocConverter
from paper_agent.tools.registry import ToolRegistry  # noqa: F401 - 保持依赖一致
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
\begin{document}
\section{Method}
We propose a model. The loss is $L = \sum_i (y_i - \hat{y}_i)^2$.
\end{document}
"""


def _require_pandoc():
    if not PandocConverter().probe():
        pytest.skip("pandoc 不可用，跳过真实转换用例")


def test_intent_binding():
    assert ConvertWorkflow().intent is Intent.CONVERT_FORMAT


def test_missing_source_reports_unresolved(tmp_path):
    """无源文件 → ok=False 且 unresolved 非空（诚实上报，不抛异常）。"""
    ctx = _ctx(tmp_path)
    result = ConvertWorkflow().run(ctx, {"to_format": "docx"})
    assert isinstance(result, WorkflowResult)
    assert result.ok is False
    assert result.unresolved
    assert "未找到" in result.unresolved[0]
    assert result.files == []


def test_unsupported_format_reports_unresolved(tmp_path):
    src = tmp_path / "p.tex"
    src.write_text(_TEX, encoding="utf-8")
    ctx = _ctx(tmp_path)
    result = ConvertWorkflow().run(ctx, {"to_format": "pdf", "source_path": str(src)})
    assert result.ok is False
    assert any("不支持" in u for u in result.unresolved)


def test_convert_produces_new_file_and_keeps_source_bytes(tmp_path):
    """转格式产物为新文件；原稿字节不变（Property 5 原稿无损）。"""
    _require_pandoc()
    pytest.importorskip("docx")
    src = tmp_path / "paper.tex"
    src.write_text(_TEX, encoding="utf-8")
    original = src.read_bytes()
    ctx = _ctx(tmp_path)

    result = ConvertWorkflow().run(
        ctx, {"to_format": "docx", "source_path": str(src), "two_column": True}
    )
    assert result.ok is True
    assert result.files
    produced = tmp_path / "paper_converted.docx"
    assert produced.exists()
    assert str(produced) in result.files
    # 原稿逐字节不变。
    assert src.read_bytes() == original


def test_two_column_applied(tmp_path):
    _require_pandoc()
    docx = pytest.importorskip("docx")
    from docx.oxml.ns import qn

    src = tmp_path / "paper.tex"
    src.write_text(_TEX, encoding="utf-8")
    ctx = _ctx(tmp_path)
    result = ConvertWorkflow().run(
        ctx, {"to_format": "docx", "source_path": str(src), "two_column": True}
    )
    assert result.ok is True
    document = docx.Document(str(tmp_path / "paper_converted.docx"))
    cols = document.sections[0]._sectPr.find(qn("w:cols"))
    assert cols is not None and cols.get(qn("w:num")) == "2"
