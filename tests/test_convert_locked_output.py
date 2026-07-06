"""输出文件被占用（Word 锁定）时的健壮性:换名重试 + 诚实提示。

用假 PandocConverter 模拟"首次 permission denied、换名后成功",不依赖真实 pandoc。
"""

from __future__ import annotations

import copy

from paper_agent.agent_platform.guardrail_gate import GuardrailGate
from paper_agent.agent_platform.models import AgentSession, WritingTask
from paper_agent.agent_platform.tools import convert_tool
from paper_agent.agent_platform.tools.context import ToolContext
from paper_agent.export.pandoc_pipeline import ConversionResult
from paper_agent.elicitation import AutoElicitor
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


class _LockedThenOkPandoc:
    """首次(原名)返回 permission denied,换名后成功。"""

    def __init__(self):
        self.calls = []

    def probe(self, **k):
        return True

    def convert_file(self, src, out_path, **k):
        self.calls.append(out_path)
        if len(self.calls) == 1:
            return ConversionResult(
                ok=False, exit_code=1,
                stderr="pandoc.exe: out.docx: withBinaryFile: permission denied (Permission denied)",
            )
        # 换名后:写出一个占位文件并成功。
        with open(out_path, "w", encoding="utf-8") as f:
            f.write("ok")
        return ConversionResult(ok=True, exit_code=0, stderr="")


class _AlwaysLockedPandoc:
    def probe(self, **k):
        return True

    def convert_file(self, src, out_path, **k):
        return ConversionResult(
            ok=False, exit_code=1,
            stderr="pandoc: xxx: permission denied (Permission denied)",
        )


def test_locked_output_retries_with_new_name(tmp_path, monkeypatch):
    src = tmp_path / "paper.tex"
    src.write_text("\\documentclass{article}\\begin{document}x\\end{document}", encoding="utf-8")
    fake = _LockedThenOkPandoc()
    monkeypatch.setattr(convert_tool, "PandocConverter", lambda: fake)

    outcome = convert_tool.convert_document_core(
        _ctx(tmp_path), to_format="markdown", path=str(src)  # markdown 跳过 docx 后处理
    )
    assert outcome.ok is True
    # 换了新名重试(两次调用,产物名不同)。
    assert len(fake.calls) == 2
    assert "可能正被 Word" in outcome.message()


def test_persistent_lock_gives_clear_hint(tmp_path, monkeypatch):
    src = tmp_path / "paper.tex"
    src.write_text("\\documentclass{article}\\begin{document}x\\end{document}", encoding="utf-8")
    monkeypatch.setattr(convert_tool, "PandocConverter", lambda: _AlwaysLockedPandoc())

    outcome = convert_tool.convert_document_core(
        _ctx(tmp_path), to_format="markdown", path=str(src)
    )
    assert outcome.ok is False
    assert "被占用" in outcome.error
    assert "Word" in outcome.error
