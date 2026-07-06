"""意图路由（intent-routing-and-workflows · Task 1）。

把用户单条请求映射到**有限意图枚举**（固定任务类型 + ``open``），供上层决定"走确定性
工作流"还是"落自由智能体"。

设计要点（见 spec）：
- **LLM 理解为主**：用 LLM 做**单选分类**（从枚举里选一个）+ 置信度 + 一句意图复述——鲁棒
  兼容各种自然语言说法；LLM 只分类、绝不编排工具，输出经防御式解析并**归一到枚举**。
- **确定性信号作加速/校验**：源文件后缀 + 少量强关键词。极强信号（有源文件 + 明确目标格式 +
  转换动词）唯一命中固定任务时可**省一次 LLM 调用**；信号与 LLM 冲突则**降低置信度**、转澄清。
  信号**不追求覆盖所有说法**。
- **降依赖在执行层**：路由只负责"选一个意图 + 抽少量参数"，不决定工具序列（那由工作流写死）。
- **绝不因路由失败拒绝服务**：任何异常 → 回退 ``Intent.OPEN``（走自由智能体）。

本模块只产出 :class:`RouteDecision`（含是否需要确认、候选意图、复述）；"低置信澄清"与"执行前
回显确认"的交互在 Task 2 实现，工作流执行在 Task 3+。
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from enum import Enum

from paper_agent.elicitation import Elicitor, Question
from paper_agent.providers.llm.base import LLMProvider, Message


class Intent(str, Enum):
    """有限意图枚举。``OPEN`` 表示无固定流程、落自由智能体。"""

    CONVERT_FORMAT = "convert_format"   # 跨格式转换（.tex↔docx↔md，可含双栏）
    INPLACE_POLISH = "inplace_polish"   # 保结构语言润色（原 .tex/.docx）
    OPEN = "open"                       # 开放任务 → 自由智能体

    @classmethod
    def fixed_tasks(cls) -> set["Intent"]:
        """固定任务类型集合（走确定性工作流的意图）。"""
        return {cls.CONVERT_FORMAT, cls.INPLACE_POLISH}


@dataclass
class RouteDecision:
    """一次路由判定的结构化结果。"""

    intent: Intent
    confidence: float = 0.0
    params: dict = field(default_factory=dict)
    signals: list[str] = field(default_factory=list)   # 命中的确定性信号（可解释）
    needs_confirmation: bool = False                    # 是否需要用户确认（澄清/回显）
    candidates: list[Intent] = field(default_factory=list)  # 低置信时供选择的候选
    rephrase: str = ""                                  # 对意图的一句话复述（回显用）


# --------------------------------------------------------------------------- #
# 确定性信号（加速 / 校验；不追求覆盖所有说法）
# --------------------------------------------------------------------------- #

# 文件扩展名 → 规范格式名（用于判断源/目标是否同格式）。
_EXT_FORMAT = {
    ".docx": "docx",
    ".tex": "latex",
    ".latex": "latex",
    ".md": "markdown",
    ".markdown": "markdown",
}


def _same_format(fmt: str, src_ext: str) -> bool:
    """目标格式与源文件扩展名是否指向同一格式（如 .docx 与 "docx"）。"""
    return bool(src_ext) and _EXT_FORMAT.get((src_ext or "").lower()) == fmt


# 目标格式关键词 → 规范格式名。
def _detect_target_format(text: str) -> str:
    if "docx" in text or "word" in text:
        return "docx"
    if "latex" in text or ".tex" in text or "tex格式" in text or "tex 格式" in text:
        return "latex"
    if "markdown" in text or ".md" in text or "md格式" in text or "md 格式" in text:
        return "markdown"
    return ""


# 从用户消息里提取文件路径（带引号优先——能处理含空格的路径；否则裸路径）。
# 以已知文档扩展名结尾，避免误抓普通词。
_QUOTED_PATH = re.compile(
    r'["\'\u201c\u201d]([^"\'\u201c\u201d\n]+?\.(?:tex|latex|docx|md|markdown))["\'\u201c\u201d]',
    re.IGNORECASE,
)
_BARE_PATH = re.compile(
    r'((?:[A-Za-z]:\\|/|\.{0,2}/)[^\s"\'\u201c\u201d]*?\.(?:tex|latex|docx|md|markdown))',
    re.IGNORECASE,
)


def _extract_source_path(request_text: str) -> str:
    """从**原始**请求文本里提取源文件路径（引号内优先，其次裸路径）；无则空串。"""
    text = request_text or ""
    m = _QUOTED_PATH.search(text)
    if m:
        return m.group(1).strip()
    m = _BARE_PATH.search(text)
    if m:
        return m.group(1).strip()
    return ""


_CONVERT_VERBS = ("转成", "转为", "转换", "转化", "导出为", "导出成", "变成", "生成", "转")
_TWO_COLUMN_KW = ("双栏", "两栏", "分栏", "two column", "two-column", "double column")
_POLISH_KW = ("润色", "语言", "表达", "通顺", "流畅", "措辞", "文笔")
_KEEP_FORMAT_KW = ("保留格式", "保结构", "保留原格式", "保留原有格式", "别改格式",
                   "不改格式", "不动格式", "原样", "保持格式", "保留排版")

# 转换确定性核心**无法覆盖**、需转交柔性通道（open→run_python / python-docx）的排版细项。
# 字体/字号关键词。
_FONT_KW = (
    "字体", "字号", "五号", "小四", "四号", "小五", "小三", "三号", "二号", "小二",
    "宋体", "楷体", "黑体", "仿宋", "雅黑", "times", "font", "磅", "pt",
)
# 图跨栏放置：如"图要双栏放置""把图搞成双栏""图横跨两栏"。
_FIGURE_SPAN_RE = re.compile(r"图.{0,6}(?:双栏|两栏|跨栏|横跨|通栏)")


def _detect_followups(text: str) -> list[str]:
    """从消息里识别转换核心覆盖不了、须转交柔性通道处理的排版细项（人可读）。

    这些不是转换核心的旋钮（核心只保证：格式转换 + 整篇双栏 + 三线表 + 表格列宽），
    识别出来是为了**诚实回报 + 兜底转交**，而非在核心里现做。
    """
    items: list[str] = []
    if _FIGURE_SPAN_RE.search(text):
        items.append("让图跨双栏放置（不要挤在单栏）")
    if any(k in text for k in _FONT_KW):
        items.append("设置正文字体/字号")
    return items


def detect_signals(request_text: str, ws) -> tuple[set[Intent], dict, list[str]]:
    """从请求文本 + 工作区确定性信号，返回 (命中意图集合, 抽取参数, 信号标签)。

    这是**加速/校验**用的先验，不是主判定——不命中很正常（交给 LLM）。
    """
    text = (request_text or "").lower()
    intents: set[Intent] = set()
    params: dict = {}
    labels: list[str] = []

    profile = getattr(ws, "profile", None) or {}
    src_ext = profile.get("source_document_ext", "")
    src_path = profile.get("source_document_path", "")
    if src_path:
        params["source_path"] = src_path
        labels.append(f"source_ext={src_ext}")

    # 消息里显式给出的路径优先（用户直接贴了原文件路径，未必 import 过）。
    msg_path = _extract_source_path(request_text or "")
    if msg_path:
        params["source_path"] = msg_path
        ext = os.path.splitext(msg_path)[1].lower()
        if ext:
            src_ext = ext
        labels.append("msg_source_path")

    # 转格式信号：有明确目标格式 + (转换动词 或 已有源文件)。
    # 关键：目标格式检测**排除已提取的源文件路径**——否则源文件名里的 ".docx"/".tex"
    # 会被误当成"要转成该格式"（用户贴的是源文件、不是要转成它的格式）。
    text_for_fmt = text
    if msg_path:
        text_for_fmt = text_for_fmt.replace(msg_path.lower(), " ")
    fmt = _detect_target_format(text_for_fmt)
    # 源与目标同格式（如 docx→docx）不是真转换 → 不作为转格式信号（多为文档内编辑请求）。
    if fmt and _same_format(fmt, src_ext):
        fmt = ""
        labels.append("same_format_noop")
    if fmt:
        params["to_format"] = fmt
        labels.append(f"target_format={fmt}")
    has_convert_verb = any(v in text for v in _CONVERT_VERBS)
    if fmt and (has_convert_verb or src_ext):
        intents.add(Intent.CONVERT_FORMAT)
        labels.append("convert_signal")

    # 双栏参数。
    if any(k in text for k in _TWO_COLUMN_KW):
        params["two_column"] = True
        labels.append("two_column")

    # 保结构润色信号：润色词 + 保留格式词同时出现。
    if any(k in text for k in _POLISH_KW) and any(k in text for k in _KEEP_FORMAT_KW):
        intents.add(Intent.INPLACE_POLISH)
        labels.append("inplace_polish_signal")

    # 核心覆盖不了的排版细项（字体/字号、图跨栏）→ 记为 followups，供工作流诚实上报 +
    # 上层兜底转交（不在确定性核心里现做）。同时留下原始请求文本供转交时给智能体上下文。
    followups = _detect_followups(text)
    if followups:
        params["followups"] = followups
        params["followup_source_text"] = request_text or ""
        labels.append("followups")

    return intents, params, labels


def _is_strong_convert(intents: set[Intent], params: dict, labels: list[str]) -> bool:
    """极强转格式信号：有源文件 + 明确目标格式 + 转换动词——可省 LLM 调用。"""
    return (
        Intent.CONVERT_FORMAT in intents
        and intents == {Intent.CONVERT_FORMAT}
        and "to_format" in params
        and "source_path" in params
    )


# --------------------------------------------------------------------------- #
# LLM 单选分类（主判定）
# --------------------------------------------------------------------------- #

_CLASSIFY_SYSTEM = (
    "你是一个意图分类器。判断用户这条请求属于以下哪一类，**只输出一个 JSON 对象**，"
    "不要输出别的：\n"
    "- convert_format：把文档从**一种文件格式转成另一种文件格式**（如 LaTeX→Word/docx、"
    "docx→LaTeX、md→docx）。**仅指跨文件格式的整体转换**。\n"
    "- inplace_polish：在**保留原文件格式/结构**的前提下润色语言表达\n"
    "- open：其它所有情况。**特别注意**：在**同一个文档内部**修改排版/字体/行距/缩进、"
    "把某张图改成双栏图或单栏图、调整某个图表、改某段的格式属性、加文献/引用、改内容、"
    "补写章节、从零写作、评审、问答等，**全部属于 open**（这些不是文件格式转换）。\n"
    "判定要点：只有当用户要把文档**从一种格式另存/转换为另一种格式**时才是 convert_format；"
    "若源文件和目标是**同一种格式**（如给了 docx 又要在 docx 里改东西），那不是转换，是 open。\n"
    "输出格式：{\"intent\": \"convert_format|inplace_polish|open\", "
    "\"confidence\": 0到1的小数, \"rephrase\": \"用一句话复述你判断的用户意图\"}"
)


def _extract_json(text: str) -> dict | None:
    """从 LLM 输出里防御式提取第一个 JSON 对象；失败返回 None。"""
    if not text:
        return None
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if not match:
        return None
    try:
        data = json.loads(match.group(0))
        return data if isinstance(data, dict) else None
    except (ValueError, TypeError):
        return None


def _normalize_intent(raw) -> Intent:
    """把 LLM 给的意图字符串归一到枚举；枚举外一律回退 OPEN。"""
    try:
        return Intent(str(raw).strip().lower())
    except (ValueError, AttributeError):
        return Intent.OPEN


def _describe(intent: Intent, params: dict) -> str:
    """为固定任务生成一句中文复述（回显确认用）。"""
    if intent is Intent.CONVERT_FORMAT:
        fmt = params.get("to_format", "目标格式")
        extra = "、双栏" if params.get("two_column") else ""
        return f"把你的文稿转换成 {fmt}{extra}"
    if intent is Intent.INPLACE_POLISH:
        return "在保留原格式的前提下润色语言"
    return "按开放任务处理"


class IntentRouter:
    """意图路由：LLM 单选分类为主 + 确定性信号加速/校验 + 用户确认兜底。"""

    def __init__(
        self,
        llm: LLMProvider,
        *,
        confidence_threshold: float = 0.75,
        always_confirm_fixed: bool = True,
    ) -> None:
        self._llm = llm
        self._threshold = confidence_threshold
        self._always_confirm_fixed = always_confirm_fixed

    def route(self, request_text: str, ws) -> RouteDecision:
        """把请求映射到意图；任何异常安全回退 ``Intent.OPEN``（绝不拒绝服务）。"""
        try:
            return self._route(request_text, ws)
        except Exception:  # noqa: BLE001 - 路由失败不拖垮服务
            return RouteDecision(
                intent=Intent.OPEN, confidence=0.0,
                rephrase="（路由异常，按开放任务处理）",
            )

    def _route(self, request_text: str, ws) -> RouteDecision:
        signal_intents, params, labels = detect_signals(request_text, ws)

        # 加速：极强转格式信号 → 直接高置信，省一次 LLM 调用（仍按需回显确认）。
        if _is_strong_convert(signal_intents, params, labels):
            conf = 0.95
            return RouteDecision(
                intent=Intent.CONVERT_FORMAT, confidence=conf, params=params,
                signals=labels, rephrase=_describe(Intent.CONVERT_FORMAT, params),
                needs_confirmation=self._needs_confirmation(Intent.CONVERT_FORMAT, conf),
                candidates=[Intent.CONVERT_FORMAT],
            )

        # LLM 单选分类（主判定）。
        intent, conf, rephrase = self._classify(request_text, labels)

        # 交叉校验：信号命中了固定任务但与 LLM 判定冲突 → 降置信、转澄清。
        conflict = bool(signal_intents) and intent not in signal_intents
        if conflict:
            conf = min(conf, 0.4)
            labels.append("signal_llm_conflict")
        elif intent in signal_intents:
            conf = max(conf, 0.85)  # 信号与 LLM 一致 → 提升置信

        # 固定任务的参数以信号抽取为准（LLM 不擅自定夺高风险参数）。
        if intent in Intent.fixed_tasks() and not rephrase:
            rephrase = _describe(intent, params)

        candidates = _ordered_candidates(signal_intents | {intent})
        needs_conf = self._needs_confirmation(intent, conf) or conflict
        return RouteDecision(
            intent=intent, confidence=conf, params=params, signals=labels,
            needs_confirmation=needs_conf, candidates=candidates, rephrase=rephrase,
        )

    def _classify(self, request_text: str, labels: list[str]) -> tuple[Intent, float, str]:
        """调 LLM 做单选分类，返回 (intent, confidence, rephrase)；解析失败回退 OPEN。"""
        signal_hint = ("；已知信号：" + ", ".join(labels)) if labels else ""
        messages = [
            Message(role="system", content=_CLASSIFY_SYSTEM),
            Message(role="user", content=(request_text or "") + signal_hint),
        ]
        try:
            resp = self._llm.complete(messages)
        except Exception:  # noqa: BLE001 - LLM 失败 → 回退 open
            return Intent.OPEN, 0.0, "（分类不可用，按开放任务处理）"
        data = _extract_json(resp.content or "")
        if not data:
            return Intent.OPEN, 0.3, ""
        intent = _normalize_intent(data.get("intent"))
        try:
            conf = float(data.get("confidence", 0.5))
        except (TypeError, ValueError):
            conf = 0.5
        conf = max(0.0, min(1.0, conf))
        rephrase = str(data.get("rephrase", "") or "")
        return intent, conf, rephrase

    def _needs_confirmation(self, intent: Intent, confidence: float) -> bool:
        """开放任务不需确认（直接走自由智能体）；固定任务按配置/置信度决定。"""
        if intent is Intent.OPEN:
            return False
        if self._always_confirm_fixed:
            return True
        return confidence < self._threshold


def _ordered_candidates(intents: set[Intent]) -> list[Intent]:
    """候选意图按固定顺序输出（稳定、可测）。"""
    order = [Intent.CONVERT_FORMAT, Intent.INPLACE_POLISH, Intent.OPEN]
    return [i for i in order if i in intents]


# --------------------------------------------------------------------------- #
# 澄清 + 回显确认（Task 2；复用 Elicitor）
# --------------------------------------------------------------------------- #

# 意图 → 面向用户的中文标签（澄清选项 / 回显文案）。
_INTENT_LABELS: dict[Intent, str] = {
    Intent.CONVERT_FORMAT: "转换文档格式",
    Intent.INPLACE_POLISH: "保留原格式润色语言",
    Intent.OPEN: "其它（按开放任务处理）",
}


@dataclass
class ConfirmOutcome:
    """确认后的结论：最终意图 + 是否继续执行。"""

    intent: Intent
    proceed: bool                          # True=按 intent 执行；False=交回用户不执行
    params: dict = field(default_factory=dict)
    message: str = ""                      # 不执行/取消时给用户的说明


def _label_to_intent(label: str) -> Intent:
    for intent, text in _INTENT_LABELS.items():
        if label == text:
            return intent
    return Intent.OPEN


def confirm_intent(
    decision: RouteDecision, elicitor: Elicitor, *, threshold: float = 0.75
) -> ConfirmOutcome:
    """据路由结果与用户交互产出最终确认结论。

    - 不需确认（开放任务 / 已足够确定且免确认）→ 直接 proceed。
    - **低置信/冲突/多候选** → 澄清：让用户在候选意图里选（非交互默认回退 open，保守）。
    - **高置信固定任务** → 回显确认：复述意图问"开始吗"，用户否定则不执行（Req 3）。
    """
    if not decision.needs_confirmation:
        return ConfirmOutcome(decision.intent, True, dict(decision.params))

    fixed_candidates = [c for c in decision.candidates if c in Intent.fixed_tasks()]
    ambiguous = decision.confidence < threshold or len(fixed_candidates) > 1
    if ambiguous:
        return _clarify(decision, elicitor, fixed_candidates)
    return _echo(decision, elicitor)


def _clarify(
    decision: RouteDecision, elicitor: Elicitor, fixed_candidates: list[Intent]
) -> ConfirmOutcome:
    """低置信澄清：在固定任务候选 + "其它"里让用户选；默认回退 open（保守）。"""
    if not fixed_candidates:
        return ConfirmOutcome(Intent.OPEN, True, dict(decision.params))
    options = [_INTENT_LABELS[c] for c in fixed_candidates] + [_INTENT_LABELS[Intent.OPEN]]
    answer = elicitor.ask(
        Question(
            id="intent_clarify",
            prompt="我不太确定你想做什么，请选择：",
            options=options,
            default=_INTENT_LABELS[Intent.OPEN],   # 非交互/未答 → 保守回退 open
        )
    )
    return ConfirmOutcome(_label_to_intent(answer), True, dict(decision.params))


def _echo(decision: RouteDecision, elicitor: Elicitor) -> ConfirmOutcome:
    """高置信固定任务回显确认：默认"开始"（非交互下高置信执行）。"""
    what = decision.rephrase or _INTENT_LABELS.get(decision.intent, "该任务")
    proceed_opt, switch_opt, cancel_opt = "开始", "换个任务（按开放处理）", "取消"
    answer = elicitor.ask(
        Question(
            id="intent_echo",
            prompt=f"我理解你要{what}，开始吗？",
            options=[proceed_opt, switch_opt, cancel_opt],
            default=proceed_opt,
        )
    )
    if answer == cancel_opt:
        return ConfirmOutcome(
            decision.intent, False, dict(decision.params),
            message="已取消。请重新描述你的需求。",
        )
    if answer == switch_opt:
        return ConfirmOutcome(Intent.OPEN, True, dict(decision.params))
    return ConfirmOutcome(decision.intent, True, dict(decision.params))


__all__ = [
    "Intent",
    "RouteDecision",
    "IntentRouter",
    "detect_signals",
    "ConfirmOutcome",
    "confirm_intent",
]
