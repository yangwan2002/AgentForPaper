"""追踪基础设施单测（Wave 1：Task 1 追踪上下文 + Task 2 三个 sink）。"""

from __future__ import annotations

import json

from paper_agent.observability.events import Event, EventKind
from paper_agent.observability.sinks import (
    CONTENT_FULL,
    CONTENT_OFF,
    CONTENT_REDACTED,
    JsonLinesSink,
    MultiSink,
    TracingSink,
)
from paper_agent.observability.tracing import current_ids, new_trace, span


class _CollectSink:
    def __init__(self):
        self.events = []

    def emit(self, event):
        self.events.append(event)


class _BoomSink:
    def emit(self, event):
        raise RuntimeError("sink boom")


# --------------------------------------------------------------------------- #
# Task 1: 追踪上下文
# --------------------------------------------------------------------------- #

def test_no_trace_has_empty_ids():
    assert current_ids() == ("", "")


def test_new_trace_assigns_trace_id():
    with new_trace() as tid:
        assert tid
        assert current_ids()[0] == tid
    assert current_ids() == ("", "")


def test_new_trace_nested_reuses_outer():
    with new_trace() as outer:
        with new_trace() as inner:
            assert inner == outer  # 复用最外层，不新建


def test_span_emits_closing_event_with_duration():
    sink = _CollectSink()
    with new_trace() as tid:
        with span(sink, "llm.complete", EventKind.LLM_RESPONSE, data={"k": 1}) as sid:
            assert sid
            assert current_ids() == (tid, sid)
    assert len(sink.events) == 1
    ev = sink.events[0]
    assert ev.message == "llm.complete"
    assert ev.span_id and ev.duration_ms is not None
    assert ev.data.get("span") == "end"
    assert ev.trace_id == tid


def test_span_parent_child_relationship():
    sink = _CollectSink()
    with new_trace():
        with span(sink, "outer", EventKind.AGENT_LOG) as outer_id:
            with span(sink, "inner", EventKind.AGENT_LOG) as inner_id:
                pass
    # 先收尾的是 inner（其 parent 是 outer）。
    inner_ev = next(e for e in sink.events if e.message == "inner")
    outer_ev = next(e for e in sink.events if e.message == "outer")
    assert inner_ev.parent_span_id == outer_id
    assert outer_ev.parent_span_id == ""
    assert inner_id != outer_id


def test_span_closes_on_exception():
    sink = _CollectSink()
    try:
        with new_trace():
            with span(sink, "boom", EventKind.AGENT_LOG):
                raise ValueError("x")
    except ValueError:
        pass
    assert len(sink.events) == 1
    assert sink.events[0].data.get("error", "").startswith("ValueError")
    # 异常后 span 栈已清空。
    assert current_ids()[1] == ""


# --------------------------------------------------------------------------- #
# Task 2: TracingSink / MultiSink / JsonLinesSink
# --------------------------------------------------------------------------- #

def test_tracing_sink_backfills_ids():
    collected = _CollectSink()
    tracing = TracingSink(collected)
    with new_trace() as tid:
        with span(collected, "s", EventKind.AGENT_LOG):
            # 普通业务事件不带 trace/span，经 TracingSink 补全。
            tracing.emit(Event(EventKind.LLM_REQUEST, message="hi"))
    ev = collected.events[0]
    assert ev.trace_id == tid
    assert ev.span_id  # 补上了当前 span
    assert ev.ts > 0


def test_tracing_sink_swallows_downstream_error():
    tracing = TracingSink(_BoomSink())
    # 下游抛异常不传播。
    tracing.emit(Event(EventKind.AGENT_LOG, message="x"))


def test_multi_sink_isolates_failure():
    good = _CollectSink()
    multi = MultiSink([_BoomSink(), good])
    multi.emit(Event(EventKind.AGENT_LOG, message="ok"))
    assert len(good.events) == 1  # 坏 sink 不影响好 sink


def test_jsonl_sink_writes_parseable_lines(tmp_path):
    path = tmp_path / "trace.jsonl"
    sink = JsonLinesSink(str(path))
    sink.emit(Event(EventKind.LLM_REQUEST, message="hello", data={"a": 1},
                    trace_id="t1", span_id="s1", ts=123.0))
    sink.emit(Event(EventKind.LLM_USAGE, message="", data={"prompt": 10},
                    trace_id="t1", span_id="s1", ts=124.0, duration_ms=5.0))
    lines = path.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 2
    rec = json.loads(lines[0])
    assert rec["trace_id"] == "t1" and rec["kind"] == "llm_request"
    assert rec["message"] == "hello" and rec["data"]["a"] == 1


def test_jsonl_redacted_truncates_content(tmp_path):
    path = tmp_path / "t.jsonl"
    sink = JsonLinesSink(str(path), content_level=CONTENT_REDACTED, redact_chars=5)
    sink.emit(Event(EventKind.LLM_REQUEST, message="0123456789",
                    data={"big": "abcdefghij", "n": 3}))
    rec = json.loads(path.read_text(encoding="utf-8").strip())
    assert rec["message"].startswith("01234") and "truncated" in rec["message"]
    assert "truncated" in rec["data"]["big"]
    assert rec["data"]["n"] == 3  # 数值不动


def test_jsonl_off_drops_content(tmp_path):
    path = tmp_path / "t.jsonl"
    sink = JsonLinesSink(str(path), content_level=CONTENT_OFF)
    sink.emit(Event(EventKind.LLM_REQUEST, message="secret text",
                    data={"prompt": 42, "big": "leak"}))
    rec = json.loads(path.read_text(encoding="utf-8").strip())
    assert rec["message"] == ""
    assert rec["data"]["prompt"] == 42  # 结构/数值保留
    assert "big" not in rec["data"]      # 字符串内容丢弃


def test_jsonl_bad_path_degrades_noop():
    # 目录不可建（用非法字符/空）时降级 no-op，不抛。
    sink = JsonLinesSink("")  # 空路径
    sink.emit(Event(EventKind.AGENT_LOG, message="x"))  # 不崩即可
