"""confirm_intent 单测（Task 2）：低置信澄清 + 高置信回显确认。"""

from __future__ import annotations

from paper_agent.agent_platform.routing import (
    ConfirmOutcome,
    Intent,
    RouteDecision,
    confirm_intent,
)
from paper_agent.elicitation import AutoElicitor, ScriptedElicitor


def _decision(**kw) -> RouteDecision:
    base = dict(intent=Intent.CONVERT_FORMAT, confidence=0.9, params={"to_format": "docx"},
                needs_confirmation=True, candidates=[Intent.CONVERT_FORMAT], rephrase="把文稿转成 docx")
    base.update(kw)
    return RouteDecision(**base)


# --- 不需确认 ---

def test_no_confirmation_proceeds_directly():
    d = _decision(needs_confirmation=False)
    out = confirm_intent(d, ScriptedElicitor())
    assert out.proceed is True and out.intent is Intent.CONVERT_FORMAT


# --- 高置信固定任务：回显确认 ---

def test_echo_confirm_start():
    d = _decision(confidence=0.95)
    out = confirm_intent(d, ScriptedElicitor({"intent_echo": "开始"}))
    assert out.proceed is True and out.intent is Intent.CONVERT_FORMAT


def test_echo_cancel_does_not_proceed():
    d = _decision(confidence=0.95)
    out = confirm_intent(d, ScriptedElicitor({"intent_echo": "取消"}))
    assert out.proceed is False
    assert out.message  # 给用户说明


def test_echo_switch_to_open():
    d = _decision(confidence=0.95)
    out = confirm_intent(d, ScriptedElicitor({"intent_echo": "换个任务（按开放处理）"}))
    assert out.proceed is True and out.intent is Intent.OPEN


def test_echo_noninteractive_defaults_to_start():
    # 非交互（高置信固定任务）→ 默认"开始"执行。
    d = _decision(confidence=0.95)
    out = confirm_intent(d, AutoElicitor())
    assert out.proceed is True and out.intent is Intent.CONVERT_FORMAT


# --- 低置信/冲突：澄清 ---

def test_low_confidence_clarify_user_picks_convert():
    d = _decision(confidence=0.3, candidates=[Intent.CONVERT_FORMAT, Intent.INPLACE_POLISH])
    out = confirm_intent(d, ScriptedElicitor({"intent_clarify": "转换文档格式"}))
    assert out.proceed is True and out.intent is Intent.CONVERT_FORMAT


def test_low_confidence_clarify_user_picks_other_open():
    d = _decision(confidence=0.3, candidates=[Intent.CONVERT_FORMAT, Intent.INPLACE_POLISH])
    out = confirm_intent(d, ScriptedElicitor({"intent_clarify": "其它（按开放任务处理）"}))
    assert out.intent is Intent.OPEN and out.proceed is True


def test_low_confidence_noninteractive_falls_back_open():
    # 非交互 + 低置信 → 保守回退 open（不擅自执行固定任务）。
    d = _decision(confidence=0.3, candidates=[Intent.CONVERT_FORMAT, Intent.INPLACE_POLISH])
    out = confirm_intent(d, AutoElicitor())
    assert out.intent is Intent.OPEN


def test_multiple_fixed_candidates_triggers_clarify():
    # 即使置信不低，多个固定任务候选也算 ambiguous → 澄清而非回显。
    d = _decision(confidence=0.9, candidates=[Intent.CONVERT_FORMAT, Intent.INPLACE_POLISH])
    out = confirm_intent(d, ScriptedElicitor({"intent_clarify": "保留原格式润色语言"}))
    assert out.intent is Intent.INPLACE_POLISH
