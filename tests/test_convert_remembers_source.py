"""转换后记住源文件到 profile（回归:第二轮"改双栏"忘了源文件 → "未找到"）。

用假 pandoc 避免依赖真实环境;核心验证:成功转换后 profile 记下 source_document_path,
且后续 detect_signals(消息里无路径)能凭 profile 定位到源文件。
"""

from __future__ import annotations

import copy

from paper_agent.agent_platform.guardrail_gate import GuardrailGate
from paper_agent.agent_platform.models import AgentSession, WritingTask
from paper_agent.agent_platform.routing import Intent, detect_signals
from paper_agent.agent_platform.tools import convert_tool
from paper_agent.agent_platform.tools.context import ToolContext
from paper_agent.elicitation import AutoElicitor
from paper_agent.export.pandoc_pipeline import ConversionResult
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


class _OkPandoc:
    def probe(self, **k):
        return True

    def convert_file(self, src, out_path, **k):
        with open(out_path, "w", encoding="utf-8") as f:
            f.write("converted")
        return ConversionResult(ok=True, exit_code=0, stderr="")


def _ctx(tmp_path):
    ws = PaperWorkspace(workspace_id="w1", input_mode=InputMode.DRAFT_REVISION)
    repo = WorkspaceRepository(_MemStore())
    repo.create(ws)
    session = AgentSession(session_id="w1", workspace=ws, task=WritingTask("t"))
    return ToolContext(
        session=session, repo=repo, gate=GuardrailGate(),
        elicitor=AutoElicitor(), output_dir=str(tmp_path),
    )


def test_convert_persists_source_to_profile(tmp_path, monkeypatch):
    src = tmp_path / "paper.tex"
    src.write_text("\\documentclass{article}\\begin{document}x\\end{document}", encoding="utf-8")
    monkeypatch.setattr(convert_tool, "PandocConverter", lambda: _OkPandoc())
    ctx = _ctx(tmp_path)

    outcome = convert_tool.convert_document_core(ctx, to_format="markdown", path=str(src))
    assert outcome.ok is True
    # 源文件已记入 profile。
    assert ctx.workspace.profile.get("source_document_path") == str(src)
    assert ctx.workspace.profile.get("source_document_ext") == ".tex"
    assert ctx.workspace.profile.get("last_output_path")


def test_followup_resolves_source_from_memory(tmp_path, monkeypatch):
    src = tmp_path / "paper.tex"
    src.write_text("\\documentclass{article}\\begin{document}x\\end{document}", encoding="utf-8")
    monkeypatch.setattr(convert_tool, "PandocConverter", lambda: _OkPandoc())
    ctx = _ctx(tmp_path)
    convert_tool.convert_document_core(ctx, to_format="markdown", path=str(src))

    # 第二轮:消息里无路径,只说"改成双栏" → 靠 profile 记忆定位源文件。
    intents, params, _labels = detect_signals("格式给我改成双栏格式", ctx.workspace)
    assert params.get("source_path") == str(src)
    assert params.get("two_column") is True
