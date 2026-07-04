"""结构化事件与事件接收器（sink）。

系统各处通过 sink.emit(event) 发出结构化事件，订阅方（控制台渲染器、
未来的前端、日志文件等）自行决定如何呈现。这样业务逻辑与展示完全解耦。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Protocol, runtime_checkable


class EventKind(str, Enum):
    WORKFLOW_START = "workflow_start"
    WORKFLOW_END = "workflow_end"
    PHASE = "phase"               # 进入某阶段（规划/检索/写作-评审/导出）
    AGENT_START = "agent_start"
    AGENT_LOG = "agent_log"       # 智能体产生的日志
    ITERATION = "iteration"       # 反馈循环轮次
    REVIEW_SCORES = "review_scores"
    LLM_REQUEST = "llm_request"
    LLM_THINKING = "llm_thinking"  # 模型思考内容（reasoning_content）
    LLM_RESPONSE = "llm_response"
    LLM_DELTA = "llm_delta"        # 流式增量（逐块输出）
    LLM_USAGE = "llm_usage"        # 单次调用 token 用量
    LLM_RETRY = "llm_retry"        # 调用失败后重试（含重试序号、计划休眠时长、异常类别）
    DEGRADATION = "degradation"    # 子功能降级（模板回退/缺图资产/跳过表格/绘图依赖不可用等，data 含 feature、reason、venue_id?）
    EXPORT_ASSET = "export_asset"  # 落盘的 Style_Asset / Figure_Asset 记录
    SPAN = "span"                  # 一个 span 的收尾事件（含 duration_ms；由 tracing.span 发出）


@dataclass
class Event:
    kind: EventKind
    message: str = ""
    data: dict = field(default_factory=dict)
    # 追踪字段（可选，默认空；由 TracingSink 用 contextvars 自动补全，未追踪时保持空）。
    # 新增字段全部可选、默认空——既有构造与断言不受影响（向后兼容）。
    trace_id: str = ""
    span_id: str = ""
    parent_span_id: str = ""
    ts: float = 0.0                     # epoch 秒；0.0 表示未追踪/未补全
    duration_ms: float | None = None    # 仅 span 收尾事件带


@runtime_checkable
class EventSink(Protocol):
    def emit(self, event: Event) -> None:
        ...


class NullSink:
    """默认空实现：丢弃所有事件（无可观测开销）。"""

    def emit(self, event: Event) -> None:  # noqa: D401
        return None


class CallbackSink:
    """把事件转发给一个回调函数，便于自定义订阅。"""

    def __init__(self, callback) -> None:
        self._cb = callback

    def emit(self, event: Event) -> None:
        self._cb(event)
