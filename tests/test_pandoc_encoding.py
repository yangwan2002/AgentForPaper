"""PandocConverter 子进程编码回归测试。

锁定根因修复：Windows 上 subprocess 若不显式指定 encoding，会用系统默认（GBK），
导致传给 pandoc 的中文 markdown 被错误编码 → docx 中文乱码。必须固定 UTF-8。
"""

from __future__ import annotations

from paper_agent.export import pandoc_pipeline
from paper_agent.export.pandoc_pipeline import PandocConverter


class _FakeCompleted:
    def __init__(self):
        self.returncode = 0
        self.stdout = "pandoc 3.10"
        self.stderr = ""


def test_convert_passes_utf8_encoding_to_subprocess(monkeypatch):
    captured = {}

    def _fake_run(args, **kwargs):
        captured.update(kwargs)
        return _FakeCompleted()

    monkeypatch.setattr(pandoc_pipeline.subprocess, "run", _fake_run)
    PandocConverter().convert("# 引言\n\n空地协同。", target="docx", out_path="x.docx")

    assert captured.get("encoding") == "utf-8", "pandoc 子进程必须固定 UTF-8，避免中文按 GBK 损坏"
    assert captured.get("input") == "# 引言\n\n空地协同。"


def test_probe_passes_utf8_encoding_to_subprocess(monkeypatch):
    captured = {}

    def _fake_run(args, **kwargs):
        captured.update(kwargs)
        return _FakeCompleted()

    monkeypatch.setattr(pandoc_pipeline.subprocess, "run", _fake_run)
    PandocConverter().probe()
    assert captured.get("encoding") == "utf-8"
