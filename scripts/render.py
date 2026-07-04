"""从已保存的工作区 JSON 渲染论文文件（无需重新生成）。

即使某次运行中途失败，工作区 JSON 也已实时落盘，可用本脚本把当前进度
渲染成 md / latex / docx。

用法：
    python scripts/render.py <workspace_id> [markdown|latex|docx]

示例：
    python scripts/render.py 6a63712a7dab markdown
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from paper_agent.export.factory import get_exporter  # noqa: E402
from paper_agent.workspace.models import OutputFormat  # noqa: E402
from paper_agent.workspace.store import JsonFileStore  # noqa: E402

WORKSPACE_DIR = "output"


def main() -> None:
    if len(sys.argv) < 2:
        print("用法：python scripts/render.py <workspace_id> [markdown|latex|docx]")
        raise SystemExit(2)

    workspace_id = sys.argv[1]
    fmt = OutputFormat(sys.argv[2]) if len(sys.argv) > 2 else None

    store = JsonFileStore(WORKSPACE_DIR)
    ws = store.load(workspace_id)
    if ws is None:
        print(f"未找到工作区：{WORKSPACE_DIR}/{workspace_id}.json")
        raise SystemExit(1)

    if fmt is not None:
        ws.output_format = fmt

    exporter = get_exporter(ws.output_format)
    result = exporter.export(ws, WORKSPACE_DIR)
    print(f"已渲染（{result.output_format.value}）：")
    for f in result.files:
        print(f"  - {f}")


if __name__ == "__main__":
    main()
