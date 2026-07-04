"""convert_document 跨格式直转工具测试（latex→docx 保公式 + 双栏）。

无 pandoc 环境跳过真实转换用例；无源/格式错误等分支不依赖 pandoc。
"""

from __future__ import annotations

import copy

import pytest

from paper_agent.agent_platform.guardrail_gate import GuardrailGate
from paper_agent.agent_platform.models import AgentSession, WritingTask
from paper_agent.agent_platform.tools.context import ToolContext
from paper_agent.agent_platform.tools.convert_tool import register_convert_document
from paper_agent.elicitation import AutoElicitor
from paper_agent.export.pandoc_pipeline import PandocConverter
from paper_agent.tools.registry import ToolRegistry
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
\begin{equation}
E = mc^2
\end{equation}
\end{document}
"""


def test_no_source_returns_error(tmp_path):
    ctx = _ctx(tmp_path)
    registry = ToolRegistry()
    register_convert_document(registry, ctx)
    out = registry.call("convert_document", to_format="docx")
    assert "未找到" in out


def test_unsupported_target(tmp_path):
    src = tmp_path / "p.tex"
    src.write_text(_TEX, encoding="utf-8")
    ctx = _ctx(tmp_path)
    registry = ToolRegistry()
    register_convert_document(registry, ctx)
    out = registry.call("convert_document", to_format="pdf", path=str(src))
    assert "不支持" in out


def _require_pandoc():
    if not PandocConverter().probe():
        pytest.skip("pandoc 不可用，跳过真实转换用例")


def test_latex_to_docx_preserves_equation(tmp_path):
    _require_pandoc()
    docx = pytest.importorskip("docx")
    src = tmp_path / "paper.tex"
    src.write_text(_TEX, encoding="utf-8")
    ctx = _ctx(tmp_path)
    registry = ToolRegistry()
    register_convert_document(registry, ctx)

    out = registry.call("convert_document", to_format="docx", path=str(src))
    assert "已直转为 docx" in out
    produced = tmp_path / "paper_converted.docx"
    assert produced.exists()
    # 产物是合法 docx，且含 Word 原生公式（OMML）——公式没被当纯文本。
    document = docx.Document(str(produced))
    xml = document.element.xml
    assert "m:oMath" in xml or "oMath" in xml  # pandoc 把 $..$/equation 转成了 OMML


def test_latex_to_docx_two_column(tmp_path):
    _require_pandoc()
    docx = pytest.importorskip("docx")
    from docx.oxml.ns import qn

    src = tmp_path / "paper.tex"
    src.write_text(_TEX, encoding="utf-8")
    ctx = _ctx(tmp_path)
    registry = ToolRegistry()
    register_convert_document(registry, ctx)

    out = registry.call(
        "convert_document", to_format="docx", path=str(src), two_column=True
    )
    assert "双栏" in out
    document = docx.Document(str(tmp_path / "paper_converted.docx"))
    cols = document.sections[0]._sectPr.find(qn("w:cols"))
    assert cols is not None and cols.get(qn("w:num")) == "2"


def test_resolves_source_from_profile(tmp_path):
    _require_pandoc()
    src = tmp_path / "imported.tex"
    src.write_text(_TEX, encoding="utf-8")
    ws = PaperWorkspace(workspace_id="w1", input_mode=InputMode.DRAFT_REVISION)
    ws.profile["source_document_path"] = str(src)
    ws.profile["source_document_ext"] = ".tex"
    ctx = _ctx(tmp_path, ws)
    registry = ToolRegistry()
    register_convert_document(registry, ctx)

    out = registry.call("convert_document", to_format="docx")  # 不传 path
    assert "已直转为 docx" in out
    assert (tmp_path / "imported_converted.docx").exists()
