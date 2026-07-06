"""意图路由从消息里提取源文件路径（回归：给了路径仍报"未找到源文件"）。"""

from __future__ import annotations

from paper_agent.agent_platform.routing import (
    Intent,
    _extract_source_path,
    _is_strong_convert,
    detect_signals,
)
from paper_agent.workspace.models import InputMode, PaperWorkspace


def _ws():
    return PaperWorkspace(workspace_id="w1", input_mode=InputMode.DRAFT_REVISION)


def test_extract_quoted_windows_path():
    text = '"D:\\Users\\yangwan\\Downloads\\新建文本文档.tex"这是我的论文，转成docx吗'
    assert _extract_source_path(text) == "D:\\Users\\yangwan\\Downloads\\新建文本文档.tex"


def test_extract_quoted_path_with_space():
    text = '"D:\\a b\\新建 文本文档.tex" 帮我转 docx'
    assert _extract_source_path(text) == "D:\\a b\\新建 文本文档.tex"


def test_extract_bare_unix_path():
    text = "把 /home/u/paper.docx 转成 latex"
    assert _extract_source_path(text) == "/home/u/paper.docx"


def test_no_path_returns_empty():
    assert _extract_source_path("帮我把论文转成 docx") == ""


def test_detect_signals_picks_message_path_and_strong_convert():
    text = '"D:\\Users\\yangwan\\Downloads\\paper.tex"这是latex版，你能帮我转成docx格式吗'
    intents, params, labels = detect_signals(text, _ws())
    assert Intent.CONVERT_FORMAT in intents
    assert params.get("source_path") == "D:\\Users\\yangwan\\Downloads\\paper.tex"
    assert params.get("to_format") == "docx"
    # 有源文件 + 目标格式 + 转换意图 → 极强转格式信号可复现。
    assert _is_strong_convert(intents, params, labels) is True


def test_message_path_overrides_profile():
    ws = _ws()
    ws.profile["source_document_path"] = "C:\\old\\imported.tex"
    ws.profile["source_document_ext"] = ".tex"
    text = '"D:\\new\\fresh.docx" 转成 latex'
    _intents, params, _labels = detect_signals(text, ws)
    assert params["source_path"] == "D:\\new\\fresh.docx"


def test_source_docx_path_not_treated_as_target_format():
    """贴了 docx 源文件路径 + 文档内编辑请求，不应把源名里的 .docx 误当成目标格式。"""
    text = '"D:\\out\\paper.docx"，帮我把这个文档里的图1改成双栏图'
    intents, params, _labels = detect_signals(text, _ws())
    # 源文件是 docx，本轮无"转成另一种格式"意图 → 不该产出 to_format，也不该命中转格式。
    assert "to_format" not in params
    assert Intent.CONVERT_FORMAT not in intents


def test_docx_to_docx_is_not_convert_signal():
    """docx→docx 的"转换"无意义，不作为转格式信号（同格式空操作）。"""
    text = '"D:\\out\\paper.docx" 帮我转成 docx'
    intents, params, labels = detect_signals(text, _ws())
    assert "to_format" not in params
    assert Intent.CONVERT_FORMAT not in intents
    assert "same_format_noop" in labels


def test_cross_format_still_detected_with_message_path():
    """跨格式仍要能识别：tex 源 → docx 目标。"""
    text = '"D:\\p\\paper.tex" 转成 docx'
    intents, params, _labels = detect_signals(text, _ws())
    assert params.get("to_format") == "docx"
    assert Intent.CONVERT_FORMAT in intents


def test_followups_detected_for_font_and_figure_span():
    """核心覆盖不了的排版细项（字体/字号、图跨栏）应被识别为 followups，供兜底转交。"""
    text = '"D:\\p\\paper.tex" 转成 docx，双栏，字体五号，图要双栏放置不要单栏'
    _intents, params, labels = detect_signals(text, _ws())
    followups = params.get("followups") or []
    assert any("图" in f for f in followups)      # 图跨栏
    assert any("字" in f for f in followups)      # 字体/字号
    assert params.get("followup_source_text")     # 原始请求文本带出供转交上下文
    assert "followups" in labels


def test_no_followups_when_only_core_covered():
    """只提核心能覆盖的（格式 + 双栏）时，不产生 followups。"""
    text = '"D:\\p\\paper.tex" 转成 docx，双栏'
    _intents, params, _labels = detect_signals(text, _ws())
    assert not params.get("followups")
