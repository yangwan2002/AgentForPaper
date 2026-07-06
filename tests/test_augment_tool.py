"""augment_document 工具测试（inplace-augment-sections · Task 3）。

覆盖：docx/tex 分派、产物为新文件、缺源报错、参考文献保留。python-docx 缺失时 docx 用例跳过。
"""

from __future__ import annotations

import copy

import pytest

from paper_agent.agent_platform.guardrail_gate import GuardrailGate
from paper_agent.agent_platform.models import AgentSession, WritingTask
from paper_agent.agent_platform.tools.augment_tool import register_augment_document
from paper_agent.agent_platform.tools.context import ToolContext
from paper_agent.elicitation import AutoElicitor
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


def _ctx(tmp_path):
    ws = PaperWorkspace(workspace_id="w1", input_mode=InputMode.DRAFT_REVISION)
    repo = WorkspaceRepository(_MemStore())
    repo.create(ws)
    session = AgentSession(session_id="w1", workspace=ws, task=WritingTask("t"))
    return ToolContext(
        session=session, repo=repo, gate=GuardrailGate(),
        elicitor=AutoElicitor(), output_dir=str(tmp_path),
    )


def _registry(ctx):
    reg = ToolRegistry()
    register_augment_document(reg, ctx)
    return reg


_TEX = "\\documentclass{article}\n\\begin{document}\n\\section{Method}\n$E=mc^2$\n\\end{document}\n"


def test_no_source_returns_error(tmp_path):
    out = _registry(_ctx(tmp_path)).call("augment_document", sections=[{"title": "引言"}])
    assert "未找到" in out


def test_latex_augment_produces_new_file(tmp_path):
    src = tmp_path / "paper.tex"
    src.write_text(_TEX, encoding="utf-8")
    original = src.read_bytes()
    out = _registry(_ctx(tmp_path)).call(
        "augment_document",
        path=str(src),
        sections=[{"title": "引言", "body": "引言正文。"}],
        references=["Alice. A Study. 2021."],
    )
    assert "已就地增补并导出" in out
    produced = tmp_path / "paper_augmented.tex"
    assert produced.exists()
    text = produced.read_text(encoding="utf-8")
    assert "\\section{引言}" in text
    assert "$E=mc^2$" in text  # 原公式逐字保留
    assert "\\begin{thebibliography}" in text
    assert src.read_bytes() == original  # 原稿只读


def test_nothing_to_augment(tmp_path):
    src = tmp_path / "paper.tex"
    src.write_text(_TEX, encoding="utf-8")
    out = _registry(_ctx(tmp_path)).call("augment_document", path=str(src))
    assert "未提供" in out


def test_docx_augment_produces_new_file(tmp_path):
    docx = pytest.importorskip("docx")
    src = tmp_path / "paper.docx"
    d = docx.Document()
    d.add_heading("方法", level=1)
    d.add_paragraph("方法正文。")
    d.save(str(src))
    out = _registry(_ctx(tmp_path)).call(
        "augment_document",
        path=str(src),
        sections=[{"title": "引言", "body": "引言正文。"}],
    )
    assert "已就地增补并导出" in out
    assert (tmp_path / "paper_augmented.docx").exists()
    doc = docx.Document(str(tmp_path / "paper_augmented.docx"))
    texts = [p.text for p in doc.paragraphs]
    assert "引言" in texts and "方法正文。" in texts
