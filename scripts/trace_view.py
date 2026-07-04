"""离线查看一份 trace JSONL：还原时间线 + 汇总指标（薄 CLI 封装）。

用法：
    python scripts/trace_view.py <trace.jsonl>
    python scripts/trace_view.py output/traces/<trace_id>.jsonl

出问题时用它定位"是哪一步"：按时间线还原各 span（缩进体现父子），高亮错误/降级/重试，
末尾汇总总耗时、总 token、LLM 调用数、工具调用数。核心逻辑见
paper_agent.observability.trace_view。
"""

from __future__ import annotations

import argparse
import os
import sys

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:  # noqa: BLE001
    pass

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from paper_agent.observability.trace_view import load_trace, render_report  # noqa: E402


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="离线查看 trace JSONL")
    p.add_argument("path", help="trace JSONL 文件路径")
    args = p.parse_args(argv)

    if not os.path.isfile(args.path):
        print(f"找不到 trace 文件：{args.path}", file=sys.stderr)
        return 2
    records = load_trace(args.path)
    if not records:
        print("（trace 为空或无法解析）")
        return 0
    print(render_report(records))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
