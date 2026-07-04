"""polish_latex_inplace 保结构工具测试（与 docx 对称）。

验证：保结构产出（preamble/宏/公式/引用逐字保留）、能从导入记录定位原 .tex、
非 tex/缺失路径给出明确错误。用返回空润色的 fake LLM → 守卫拦截全部 → 产物逐字节
等于原文（证明保结构不破坏）。
"""

from __future__ import annotations

import copy

from paper_agent.agent_platform.guardrail_gate import GuardrailGate
from paper_agent.agent_platform.models import AgentSession, WritingTask
from paper_agent.agent_platform.tools.context import ToolContext
from paper_agent.agent_platform.tools.latex_inplace_tool import (
    register_polish_latex_inplace,
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


class _NoopLLM:
    """返回空内容 → 守卫拦截 → 保留原文（验证保结构不破坏）。"""

    def complete(self, messages, **opts):
        return LLMResponse(content="")


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


def _ctx(tmp_path, ws=None):
    ws = ws or PaperWorkspace(workspace_id="w1", input_mode=InputMode.DRAFT_REVISION)
    repo = WorkspaceRepository(_MemStore())
    repo.create(ws)
    session = AgentSession(session_id="w1", workspace=ws, task=WritingTask("t"))
    return ToolContext(
        session=session, repo=repo, gate=GuardrailGate(),
        elicitor=AutoElicitor(), output_dir=str(tmp_path),
    )


def test_no_source_tex_returns_error(tmp_path):
    ctx = _ctx(tmp_path)
    registry = ToolRegistry()
    register_polish_latex_inplace(registry, ctx, _NoopLLM())
    out = registry.call("polish_latex_inplace")
    assert "未找到" in out


def test_preserves_structure_byte_for_byte(tmp_path):
    src = tmp_path / "paper.tex"
    src.write_text(_TEX, encoding="utf-8")
    ctx = _ctx(tmp_path)
    registry = ToolRegistry()
    register_polish_latex_inplace(registry, ctx, _NoopLLM())

    out = registry.call("polish_latex_inplace", path=str(src))
    assert "已保结构润色并导出" in out
    produced = (tmp_path / "paper_inplace.tex").read_text(encoding="utf-8")
    # 守卫拦截空润色 → 逐字节等于原文（结构与内容全保留）。
    assert produced == _TEX
    # 关键结构逐字保留。
    assert r"\newcommand{\mycmd}[1]{\textbf{#1}}" in produced
    assert r"\cite{smith2020}" in produced
    assert r"E = mc^2" in produced


def test_resolves_source_from_import_profile(tmp_path):
    src = tmp_path / "imported.tex"
    src.write_text(_TEX, encoding="utf-8")
    ws = PaperWorkspace(workspace_id="w1", input_mode=InputMode.DRAFT_REVISION)
    ws.profile["source_document_path"] = str(src)
    ws.profile["source_document_ext"] = ".tex"
    ctx = _ctx(tmp_path, ws)
    registry = ToolRegistry()
    register_polish_latex_inplace(registry, ctx, _NoopLLM())

    out = registry.call("polish_latex_inplace")  # 不传 path，走 profile
    assert "已保结构润色并导出" in out
    assert (tmp_path / "imported_inplace.tex").exists()


def test_non_tex_path_rejected(tmp_path):
    other = tmp_path / "notes.md"
    other.write_text("# hello", encoding="utf-8")
    ctx = _ctx(tmp_path)
    registry = ToolRegistry()
    register_polish_latex_inplace(registry, ctx, _NoopLLM())
    out = registry.call("polish_latex_inplace", path=str(other))
    assert "未找到" in out
