"""批次 4B：单一入口决策（entry）+ 批量澄清（ask_batch）+ CLI 输入分类。"""

from __future__ import annotations

import sys
import os

import pytest

from paper_agent.elicitation import (
    AutoElicitor,
    CLIElicitor,
    Question,
    ScriptedElicitor,
)
from paper_agent.entry import (
    Engine,
    decide_engine,
    default_output_format,
    looks_like_draft,
)
from paper_agent.workspace.models import OutputFormat


# --- entry.decide_engine ---


@pytest.mark.parametrize(
    "path,expected",
    [
        ("a.tex", Engine.LATEX_INPLACE),
        ("a.latex", Engine.LATEX_INPLACE),
        ("a.docx", Engine.DOCX_INPLACE),
        ("a.md", Engine.PIPELINE),
        ("a.pdf", Engine.PIPELINE),
        ("a.txt", Engine.PIPELINE),
        (None, Engine.PIPELINE),
    ],
)
def test_decide_engine(path, expected):
    assert decide_engine(path) is expected


def test_rebuild_forces_pipeline():
    assert decide_engine("a.tex", rebuild=True) is Engine.PIPELINE
    assert decide_engine("a.docx", rebuild=True) is Engine.PIPELINE


@pytest.mark.parametrize(
    "path,fmt",
    [
        ("a.tex", OutputFormat.LATEX),
        ("a.docx", OutputFormat.DOCX),
        ("a.md", OutputFormat.MARKDOWN),
        ("a.pdf", OutputFormat.MARKDOWN),
        (None, OutputFormat.MARKDOWN),
    ],
)
def test_default_output_format_equals_input(path, fmt):
    assert default_output_format(path) is fmt


def test_looks_like_draft_by_ext():
    assert looks_like_draft("paper.docx") is True
    assert looks_like_draft("some topic about slam") is False
    assert looks_like_draft(None) is False


def test_looks_like_draft_existing_file(tmp_path):
    f = tmp_path / "note.unknownext"
    f.write_text("x", encoding="utf-8")
    assert looks_like_draft(str(f)) is True  # 真实文件即视为初稿


# --- ask_batch ---


def test_auto_ask_batch_returns_defaults():
    e = AutoElicitor()
    qs = [Question("a", "?", default="da"), Question("b", "?", options=["x", "y"], default="y")]
    assert e.ask_batch(qs) == {"a": "da", "b": "y"}


def test_scripted_ask_batch_by_id():
    e = ScriptedElicitor({"a": "A", "b": "B"})
    qs = [Question("a", "?"), Question("b", "?")]
    assert e.ask_batch(qs) == {"a": "A", "b": "B"}


def test_cli_ask_batch_prints_header_and_collects():
    outputs = []
    inputs = iter(["1", "hello"])
    e = CLIElicitor(input_fn=lambda _p: next(inputs), output_fn=outputs.append)
    qs = [Question("scope", "范围?", options=["只润色", "补章节"], default="只润色"),
          Question("free", "自由?")]
    ans = e.ask_batch(qs)
    assert ans == {"scope": "只润色", "free": "hello"}
    assert any("需要确认 2 个问题" in o for o in outputs)


# --- CLI 输入分类 ---


def _load_run_real():
    import importlib.util

    path = os.path.join(
        os.path.dirname(__file__), "..", "scripts", "run_real.py"
    )
    spec = importlib.util.spec_from_file_location("run_real_cli", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_cli_classifies_file_as_draft_and_topic_as_topic():
    rr = _load_run_real()
    args = rr._parse_args(["mypaper.docx"])
    draft, topic = rr._classify_input(args)
    assert draft == "mypaper.docx" and topic is None

    args2 = rr._parse_args(["图像匹配研究"])
    draft2, topic2 = rr._classify_input(args2)
    assert draft2 is None and topic2 == "图像匹配研究"


def test_cli_explicit_draft_flag_wins():
    rr = _load_run_real()
    args = rr._parse_args(["某主题", "--draft", "x.md"])
    draft, topic = rr._classify_input(args)
    assert draft == "x.md" and topic is None


def test_cli_yes_flag_parsed():
    rr = _load_run_real()
    args = rr._parse_args(["x.md", "--yes"])
    assert args.yes is True


def test_cli_ingestion_confirmation_is_interactive_only(tmp_path, monkeypatch):
    rr = _load_run_real()
    draft = tmp_path / "unstructured.md"
    draft.write_text("Readable academic prose. " * 220, encoding="utf-8")

    with pytest.raises(SystemExit, match="非交互模式拒绝"):
        rr._confirm_draft_ingestion(str(draft), interactive=False)

    monkeypatch.setattr("builtins.input", lambda _prompt: "y")
    assert rr._confirm_draft_ingestion(str(draft), interactive=True) is True


def test_cli_preflight_rejects_corruption_before_dispatch(tmp_path):
    rr = _load_run_real()
    broken = tmp_path / "broken.tex"
    broken.write_text("\ufffd" * 120, encoding="utf-8")

    with pytest.raises(SystemExit, match="摄入质量检查失败"):
        rr._confirm_draft_ingestion(str(broken), interactive=True)
