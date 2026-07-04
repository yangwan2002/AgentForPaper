"""agent-observability-tracing 属性测试（Property 1-7）+ 集成 + trace_view 单测。"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

from hypothesis import given, settings
from hypothesis import strategies as st

from paper_agent.observability.events import Event, EventKind, NullSink
from paper_agent.observability.sinks import (
    CONTENT_OFF,
    CONTENT_REDACTED,
    JsonLinesSink,
    MultiSink,
    TracingSink,
)
from paper_agent.observability.tracing import current_ids, new_trace, span
from paper_agent.observability.trace_view import (
    load_trace,
    render_report,
    span_depths,
    summarize,
)

_KINDS = list(EventKind)


class _Collect:
    def __init__(self):
        self.events = []

    def emit(self, event):
        self.events.append(event)


class _Boom:
    def emit(self, event):
        raise RuntimeError("boom")


# --------------------------------------------------------------------------- #
# Property 1: 向后兼容（未开启即不变）
# --------------------------------------------------------------------------- #

def test_p1_event_defaults_and_untraced():
    e = Event(EventKind.AGENT_LOG, message="x")
    assert e.trace_id == "" and e.span_id == "" and e.parent_span_id == ""
    assert e.ts == 0.0 and e.duration_ms is None
    # 不在 trace 内经 TracingSink：trace/span 字段仍为空。
    col = _Collect()
    TracingSink(col).emit(Event(EventKind.AGENT_LOG))
    assert col.events[0].trace_id == "" and col.events[0].span_id == ""


# --------------------------------------------------------------------------- #
# Property 2: Trace 归拢
# --------------------------------------------------------------------------- #

@settings(max_examples=40)
@given(st.lists(st.sampled_from(_KINDS), min_size=1, max_size=8))
def test_p2_trace_grouping(kinds):
    col = _Collect()
    sink = TracingSink(col)
    with new_trace() as tid:
        for k in kinds:
            sink.emit(Event(k))
    assert tid
    assert all(e.trace_id == tid for e in col.events)


# --------------------------------------------------------------------------- #
# Property 3: Span 父子与闭合
# --------------------------------------------------------------------------- #

def _nest(sink, depth: int) -> None:
    if depth <= 0:
        return
    with span(sink, f"lvl{depth}"):
        _nest(sink, depth - 1)


@settings(max_examples=30)
@given(st.integers(min_value=1, max_value=5))
def test_p3_span_nesting_and_closure(depth):
    col = _Collect()
    with new_trace():
        _nest(col, depth)
    assert len(col.events) == depth
    assert all(e.duration_ms is not None for e in col.events)
    # 恰有一个 root（parent 为空），其余都有非空 parent。
    roots = [e for e in col.events if not e.parent_span_id]
    assert len(roots) == 1


def test_p3_span_closes_on_exception():
    col = _Collect()
    try:
        with new_trace():
            with span(col, "boom"):
                raise ValueError("x")
    except ValueError:
        pass
    assert len(col.events) == 1 and col.events[0].data.get("error")
    assert current_ids()[1] == ""  # 栈已清空


# --------------------------------------------------------------------------- #
# Property 4: 可观测不拖垮业务
# --------------------------------------------------------------------------- #

@settings(max_examples=40)
@given(st.lists(st.sampled_from(_KINDS), min_size=1, max_size=6))
def test_p4_sink_failure_isolated(kinds):
    good = _Collect()
    sink = TracingSink(MultiSink([_Boom(), good]))
    with new_trace():
        for k in kinds:
            sink.emit(Event(k))  # 坏 sink 不抛、不影响 good
    assert len(good.events) == len(kinds)


# --------------------------------------------------------------------------- #
# Property 5: JSONL 可解析且字段完备
# --------------------------------------------------------------------------- #

@settings(max_examples=30)
@given(st.lists(st.sampled_from(_KINDS), min_size=1, max_size=6))
def test_p5_jsonl_parseable(kinds):
    with tempfile.TemporaryDirectory() as d:
        sink = TracingSink(MultiSink([JsonLinesSink(directory=d)]))
        with new_trace() as tid:
            for k in kinds:
                sink.emit(Event(k, message="m"))
        path = os.path.join(d, f"{tid}.jsonl")
        lines = open(path, encoding="utf-8").read().strip().splitlines()
        assert len(lines) == len(kinds)
        for ln in lines:
            rec = json.loads(ln)
            for f in ("ts", "trace_id", "span_id", "parent_span_id", "kind"):
                assert f in rec


# --------------------------------------------------------------------------- #
# Property 6: 内容级别隔离
# --------------------------------------------------------------------------- #

@settings(max_examples=40)
@given(st.text(max_size=200))
def test_p6_content_levels(text):
    suffix_len = len("…[truncated]")
    with tempfile.TemporaryDirectory() as d:
        r = JsonLinesSink(
            os.path.join(d, "r.jsonl"), content_level=CONTENT_REDACTED, redact_chars=10
        )
        r.emit(Event(EventKind.LLM_REQUEST, message=text, data={"big": text, "n": 1}))
        rec = json.loads(open(os.path.join(d, "r.jsonl"), encoding="utf-8").read().strip())
        assert len(rec["message"]) <= 10 + suffix_len
        assert rec["data"]["n"] == 1  # 数值保留

        o = JsonLinesSink(os.path.join(d, "o.jsonl"), content_level=CONTENT_OFF)
        o.emit(Event(EventKind.LLM_REQUEST, message=text, data={"big": text, "n": 1}))
        rec2 = json.loads(open(os.path.join(d, "o.jsonl"), encoding="utf-8").read().strip())
        assert rec2["message"] == ""
        assert "big" not in rec2["data"] and rec2["data"]["n"] == 1


# --------------------------------------------------------------------------- #
# Property 7: 并行隔离
# --------------------------------------------------------------------------- #

def test_p7_parallel_trace_isolation():
    from paper_agent.agent_platform.subagents import run_parallel

    def task():
        with new_trace() as tid:
            with span(NullSink(), "s"):
                return tid

    results = run_parallel([task, task, task], max_workers=3)
    tids = [r.value for r in results if r.ok]
    assert len(tids) == 3 and len(set(tids)) == 3  # 各自独立 trace，互不串扰


# --------------------------------------------------------------------------- #
# 集成：开启追踪跑一次 mock 任务，产出可解析 JSONL + span 树完整
# --------------------------------------------------------------------------- #

def test_traced_run_end_to_end(tmp_path):
    from paper_agent.agent_platform.models import AgentSession, WritingTask
    from paper_agent.agent_platform.task_agent import TaskAgent
    from paper_agent.observability.llm_wrapper import ObservableLLMProvider
    from paper_agent.providers.llm.base import LLMResponse, ToolCall
    from paper_agent.tools.registry import ToolRegistry
    from paper_agent.workspace.models import InputMode, PaperWorkspace

    class _LLM:
        def __init__(self):
            self._i = 0

        def complete(self, messages, **opts):
            self._i += 1
            if self._i == 1:
                return LLMResponse(content="", tool_calls=[ToolCall(
                    id="c1", name="noop", arguments={"x": "1"})])
            return LLMResponse(content="完成。")

    sink = TracingSink(MultiSink([JsonLinesSink(directory=str(tmp_path))]))
    registry = ToolRegistry()
    registry.register("noop", "测试", lambda x=None: f"ok:{x}",
                      {"type": "object", "properties": {"x": {"type": "string"}}, "required": []})
    obs = ObservableLLMProvider(_LLM(), sink)
    agent = TaskAgent(obs, registry, sink=sink)

    ws = PaperWorkspace(workspace_id="w1", input_mode=InputMode.DRAFT_REVISION)
    session = AgentSession(session_id="w1", workspace=ws, task=WritingTask("做点事"))
    agent.run(session)

    files = list(Path(tmp_path).glob("*.jsonl"))
    assert files, "应产出 trace 文件"
    records = load_trace(str(files[0]))
    trace_ids = {r["trace_id"] for r in records if r["trace_id"]}
    assert len(trace_ids) == 1  # 整次运行同一 trace
    assert any(r["kind"] == "span" and r.get("message") == "llm.complete" for r in records)
    assert any(r.get("message", "").startswith("tool.") for r in records)
    s = summarize(records)
    assert s["llm_calls"] >= 1 and s["tool_calls"] >= 1


# --------------------------------------------------------------------------- #
# trace_view 汇总与时间线
# --------------------------------------------------------------------------- #

def test_trace_view_summarize_depths_report():
    records = [
        {"ts": 1.0, "trace_id": "t", "span_id": "a", "parent_span_id": "",
         "kind": "span", "message": "llm.complete", "duration_ms": 10, "data": {}},
        {"ts": 1.1, "trace_id": "t", "span_id": "b", "parent_span_id": "a",
         "kind": "span", "message": "tool.noop", "duration_ms": 5, "data": {}},
        {"ts": 1.0, "trace_id": "t", "span_id": "a", "parent_span_id": "",
         "kind": "llm_usage", "message": "", "data": {"prompt": 10, "completion": 5}},
        {"ts": 2.0, "trace_id": "t", "span_id": "", "parent_span_id": "",
         "kind": "degradation", "message": "降级了", "data": {}},
    ]
    s = summarize(records)
    assert s["llm_calls"] == 1 and s["tool_calls"] == 1
    assert s["total_tokens"] == 15 and s["degradations"] == 1
    assert s["total_duration_ms"] == 1000.0  # (2.0-1.0)*1000
    depths = span_depths(records)
    assert depths["a"] == 0 and depths["b"] == 1
    report = render_report(records)
    assert "汇总" in report and "llm.complete" in report
