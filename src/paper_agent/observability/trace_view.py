"""离线 trace 归因：读取一份 trace JSONL，还原时间线并汇总关键指标。

纯读、零依赖。核心逻辑（`load_trace` / `span_depths` / `summarize` / `render_report`）
放在此处便于单测；`scripts/trace_view.py` 只是薄 CLI 封装。

用途：出问题时定位"是哪一步"——按时间线还原各 span（缩进体现父子），高亮错误/降级/
重试，并汇总总耗时、总 token、LLM 调用数、工具调用数。
"""

from __future__ import annotations

import json

from paper_agent.observability.events import EventKind


def load_trace(path: str) -> list[dict]:
    """读 JSONL，逐行解析为 dict；坏行跳过。返回按 ts 升序排序的记录列表。"""
    records: list[dict] = []
    try:
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    records.append(json.loads(line))
                except (ValueError, TypeError):
                    continue
    except OSError:
        return []
    records.sort(key=lambda r: r.get("ts") or 0.0)
    return records


def span_depths(records: list[dict]) -> dict[str, int]:
    """据 SPAN 收尾事件的 span_id/parent_span_id 计算每个 span 的树深度。"""
    parent: dict[str, str] = {}
    for r in records:
        if r.get("kind") == EventKind.SPAN.value and r.get("span_id"):
            parent[r["span_id"]] = r.get("parent_span_id") or ""
    depths: dict[str, int] = {}

    def _depth(sid: str, seen: frozenset) -> int:
        if not sid or sid not in parent:
            return 0
        if sid in seen:  # 防御式：防环
            return 0
        p = parent[sid]
        return 1 + _depth(p, seen | {sid}) if p else 0

    for sid in parent:
        depths[sid] = _depth(sid, frozenset())
    return depths


def summarize(records: list[dict]) -> dict:
    """汇总关键指标：总耗时、总 token、LLM/工具调用数、降级/重试/错误数。"""
    llm_calls = tool_calls = degradations = retries = errors = 0
    total_tokens = 0
    ts_values = [r.get("ts") for r in records if r.get("ts")]

    for r in records:
        kind = r.get("kind")
        data = r.get("data") or {}
        if kind == EventKind.SPAN.value:
            msg = r.get("message") or ""
            if msg in ("llm.complete", "llm.stream"):
                llm_calls += 1
            elif msg.startswith("tool."):
                tool_calls += 1
            if data.get("error"):
                errors += 1
        elif kind == EventKind.LLM_USAGE.value:
            total_tokens += int(data.get("prompt") or 0) + int(data.get("completion") or 0)
        elif kind == EventKind.DEGRADATION.value:
            degradations += 1
        elif kind == EventKind.LLM_RETRY.value:
            retries += 1

    total_duration_ms = (
        (max(ts_values) - min(ts_values)) * 1000.0 if len(ts_values) >= 2 else 0.0
    )
    return {
        "records": len(records),
        "total_duration_ms": round(total_duration_ms, 1),
        "total_tokens": total_tokens,
        "llm_calls": llm_calls,
        "tool_calls": tool_calls,
        "degradations": degradations,
        "retries": retries,
        "errors": errors,
    }


# 需要高亮的异常信号 kind。
_HIGHLIGHT_KINDS = {EventKind.DEGRADATION.value, EventKind.LLM_RETRY.value}


def _is_anomaly(record: dict) -> bool:
    if record.get("kind") in _HIGHLIGHT_KINDS:
        return True
    return bool((record.get("data") or {}).get("error"))


def format_timeline(records: list[dict]) -> str:
    """把记录按时间线渲染为多行字符串；span 按树深度缩进，异常行前缀 ``!``。"""
    depths = span_depths(records)
    lines: list[str] = []
    for r in records:
        kind = r.get("kind", "")
        sid = r.get("span_id", "")
        depth = depths.get(sid, 0)
        indent = "  " * depth
        mark = "!" if _is_anomaly(r) else " "
        msg = (r.get("message") or "").replace("\n", " ")
        if len(msg) > 80:
            msg = msg[:80] + "…"
        suffix = ""
        if r.get("duration_ms") is not None:
            suffix = f"  ({r['duration_ms']:.0f}ms)"
        lines.append(f"{mark} {indent}[{kind}] {msg}{suffix}")
    return "\n".join(lines)


def render_report(records: list[dict]) -> str:
    """完整报告：时间线 + 指标汇总。"""
    s = summarize(records)
    timeline = format_timeline(records)
    summary = (
        "\n=== 汇总 ===\n"
        f"事件数 {s['records']}｜总耗时 {s['total_duration_ms']}ms｜"
        f"总 token {s['total_tokens']}\n"
        f"LLM 调用 {s['llm_calls']}｜工具调用 {s['tool_calls']}｜"
        f"降级 {s['degradations']}｜重试 {s['retries']}｜错误 {s['errors']}"
    )
    return timeline + "\n" + summary


__all__ = [
    "load_trace",
    "span_depths",
    "summarize",
    "format_timeline",
    "render_report",
]
