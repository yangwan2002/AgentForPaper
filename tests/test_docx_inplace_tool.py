"""polish_docx_inplace 保结构工具测试（P0-2）。

验证：工具产出保结构 docx（正文文字保留、原结构保留）、能从导入记录定位原 .docx、
非 docx/缺失路径给出明确错误。真实 docx 依赖 python-docx，缺失则跳过。
"""

from __future__ import annotations

import copy

import pytest

from paper_agent.agent_platform.guardrail_gate import GuardrailGate
from paper_agent.agent_platform.models import AgentSession, WritingTask
from paper_agent.agent_platform.tools.context import ToolContext
from paper_agent.agent_platform.tools.docx_inplace_tool import (
    register_polish_docx_inplace,
)
from paper_agent.elicitation import AutoElicitor
from paper_agent.providers.llm.base import LLMResponse
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


class _LLM:
    def complete(self, messages, **opts):
        return LLMResponse(content="（不应被调用）")


def _ctx(tmp_path, ws=None):
    ws = ws or PaperWorkspace(workspace_id="w1", input_mode=InputMode.DRAFT_REVISION)
    repo = WorkspaceRepository(_MemStore())
    repo.create(ws)
    session = AgentSession(session_id="w1", workspace=ws, task=WritingTask("t"))
    return ToolContext(
        session=session, repo=repo, gate=GuardrailGate(),
        elicitor=AutoElicitor(), output_dir=str(tmp_path),
    )


def _make_docx(path, paragraphs):
    docx = pytest.importorskip("docx")
    document = docx.Document()
    for text in paragraphs:
        document.add_paragraph(text)
    document.save(str(path))


def test_no_source_docx_returns_error(tmp_path):
    ctx = _ctx(tmp_path)
    registry = ToolRegistry()
    register_polish_docx_inplace(registry, ctx, _LLM())
    out = registry.call("polish_docx_inplace")
    assert "未找到" in out


def test_preserves_structure_copy_when_no_polish(tmp_path):
    docx = pytest.importorskip("docx")
    src = tmp_path / "orig.docx"
    _make_docx(src, ["这是正文第一段，内容足够长用于测试保结构。", "第二段正文内容。"])

    ctx = _ctx(tmp_path)
    registry = ToolRegistry()
    register_polish_docx_inplace(registry, ctx, _LLM())
    out = registry.call("polish_docx_inplace", path=str(src), polish_language=False)

    assert "已保结构处理并导出" in out
    # 产物存在且正文文字保留。
    produced = tmp_path / "orig_inplace.docx"
    assert produced.exists()
    doc = docx.Document(str(produced))
    texts = [p.text for p in doc.paragraphs]
    assert "这是正文第一段，内容足够长用于测试保结构。" in texts


def test_resolves_source_from_import_profile(tmp_path):
    docx = pytest.importorskip("docx")
    src = tmp_path / "imported.docx"
    _make_docx(src, ["从导入记录定位原文件的正文段落内容。"])

    ws = PaperWorkspace(workspace_id="w1", input_mode=InputMode.DRAFT_REVISION)
    ws.profile["source_document_path"] = str(src)
    ws.profile["source_document_ext"] = ".docx"
    ctx = _ctx(tmp_path, ws)
    registry = ToolRegistry()
    register_polish_docx_inplace(registry, ctx, _LLM())

    out = registry.call("polish_docx_inplace")  # 不传 path，走 profile
    assert "已保结构处理并导出" in out
    assert (tmp_path / "imported_inplace.docx").exists()


def test_applies_saved_typesetting(tmp_path):
    docx = pytest.importorskip("docx")
    src = tmp_path / "orig.docx"
    _make_docx(src, ["需要应用两端对齐与行距的正文段落内容，足够长。"])

    ws = PaperWorkspace(workspace_id="w1", input_mode=InputMode.DRAFT_REVISION)
    ws.profile["typesetting"] = {"alignment": "justify", "line_spacing": 22}
    ctx = _ctx(tmp_path, ws)
    registry = ToolRegistry()
    register_polish_docx_inplace(registry, ctx, _LLM())

    out = registry.call("polish_docx_inplace", path=str(src))
    assert "排版" in out
    # 核对排版已应用（复用验收检查）。
    from paper_agent.agent_platform.acceptance import check_typesetting_applied

    finding = check_typesetting_applied(
        str(tmp_path / "orig_inplace.docx"), {"alignment": "justify", "line_spacing": 22}
    )
    assert finding.ok is True
