"""IntentRouter 单测（Task 1）：LLM 单选分类 + 确定性信号加速/校验 + 回退。"""

from __future__ import annotations

from paper_agent.agent_platform.routing import (
    Intent,
    IntentRouter,
    RouteDecision,
    detect_signals,
)
from paper_agent.providers.llm.base import LLMResponse
from paper_agent.workspace.models import InputMode, PaperWorkspace


class _LLM:
    """按预置 JSON 内容返回；记录是否被调用（验证加速路径省调用）。"""

    def __init__(self, content: str):
        self._content = content
        self.calls = 0

    def complete(self, messages, **opts):
        self.calls += 1
        return LLMResponse(content=self._content)


class _BoomLLM:
    def complete(self, messages, **opts):
        raise RuntimeError("llm down")


def _ws(*, ext: str = "", path: str = "") -> PaperWorkspace:
    ws = PaperWorkspace(workspace_id="w", input_mode=InputMode.DRAFT_REVISION)
    if ext:
        ws.profile["source_document_ext"] = ext
    if path:
        ws.profile["source_document_path"] = path
    return ws


def _classify_json(intent: str, conf: float = 0.9, rephrase: str = "x") -> str:
    return f'{{"intent": "{intent}", "confidence": {conf}, "rephrase": "{rephrase}"}}'


# --------------------------------------------------------------------------- #
# 意图封闭 + LLM 归一
# --------------------------------------------------------------------------- #

def test_llm_intent_normalized_to_enum():
    router = IntentRouter(_LLM(_classify_json("convert_format")))
    d = router.route("帮我把这个稿子弄成 word", _ws())
    assert d.intent is Intent.CONVERT_FORMAT
    assert d.intent in set(Intent)


def test_llm_out_of_enum_falls_back_to_open():
    router = IntentRouter(_LLM(_classify_json("translate_to_english")))
    d = router.route("随便什么", _ws())
    assert d.intent is Intent.OPEN  # 枚举外 → 归一为 open


def test_unparseable_llm_output_falls_back():
    router = IntentRouter(_LLM("我觉得你大概想转格式吧（没有 JSON）"))
    d = router.route("处理下这个文件", _ws())
    assert d.intent is Intent.OPEN


def test_llm_exception_routes_open():
    router = IntentRouter(_BoomLLM())
    d = router.route("把它转成 docx", _ws())
    assert d.intent is Intent.OPEN  # 异常绝不拒绝服务


# --------------------------------------------------------------------------- #
# 确定性信号：加速 / 校验 / 冲突
# --------------------------------------------------------------------------- #

def test_strong_signal_accelerates_without_llm():
    # 有源文件(.tex) + 目标格式(docx) + 转换动词 → 极强信号，省 LLM 调用。
    llm = _LLM(_classify_json("open"))  # 即便 LLM 会说 open，也不该被调用
    router = IntentRouter(llm)
    ws = _ws(ext=".tex", path="D:/paper.tex")
    d = router.route("把这个 tex 转成 docx，双栏", ws)
    assert d.intent is Intent.CONVERT_FORMAT
    assert d.confidence >= 0.9
    assert d.params.get("to_format") == "docx"
    assert d.params.get("two_column") is True
    assert llm.calls == 0  # 加速路径未调 LLM


def test_signal_llm_agreement_boosts_confidence():
    # 无源文件路径（不触发加速），信号命中 convert + LLM 也判 convert → 提升置信。
    router = IntentRouter(_LLM(_classify_json("convert_format", conf=0.6)))
    d = router.route("转成 docx", _ws())
    assert d.intent is Intent.CONVERT_FORMAT
    assert d.confidence >= 0.85


def test_signal_llm_conflict_lowers_confidence_and_needs_confirm():
    # 信号命中 convert（"转成 docx"），但 LLM 判 inplace_polish → 冲突降置信、需确认。
    router = IntentRouter(_LLM(_classify_json("inplace_polish", conf=0.9)))
    d = router.route("转成 docx", _ws())
    assert d.confidence <= 0.4
    assert d.needs_confirmation is True
    assert Intent.CONVERT_FORMAT in d.candidates


# --------------------------------------------------------------------------- #
# 确认策略
# --------------------------------------------------------------------------- #

def test_open_task_needs_no_confirmation():
    router = IntentRouter(_LLM(_classify_json("open", conf=0.9)))
    d = router.route("帮我把方法章节写得更有说服力", _ws())
    assert d.intent is Intent.OPEN
    assert d.needs_confirmation is False


def test_fixed_task_confirms_by_default():
    router = IntentRouter(_LLM(_classify_json("convert_format", conf=0.99)))
    d = router.route("转成 word", _ws())
    assert d.needs_confirmation is True  # always_confirm_fixed 默认 True


def test_fixed_task_high_conf_skips_confirm_when_configured():
    router = IntentRouter(
        _LLM(_classify_json("convert_format", conf=0.99)), always_confirm_fixed=False
    )
    d = router.route("转成 word", _ws())
    assert d.needs_confirmation is False  # 高置信 + 关闭强制确认


# --------------------------------------------------------------------------- #
# detect_signals 纯函数
# --------------------------------------------------------------------------- #

def test_detect_signals_convert():
    intents, params, labels = detect_signals("把它转成 docx", _ws(ext=".tex", path="p.tex"))
    assert Intent.CONVERT_FORMAT in intents
    assert params["to_format"] == "docx"


def test_detect_signals_inplace_polish():
    intents, _p, _l = detect_signals("保留格式帮我润色一下语言", _ws())
    assert Intent.INPLACE_POLISH in intents


def test_detect_signals_none():
    intents, _p, _l = detect_signals("给相关工作加三篇文献", _ws())
    assert intents == set()  # 无固定任务信号 → 交给 LLM
