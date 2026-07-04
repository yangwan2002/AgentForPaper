"""从 LLM 文本输出中稳健地提取 JSON。

LLM 常把 JSON 包在 ```json 代码块里，或在 JSON 前后附带说明文字。
此工具尽量从中抽取出第一个合法的 JSON 对象/数组；失败返回 None，
便于调用方回退到确定性逻辑（例如 mock provider 的非 JSON 输出）。
"""

from __future__ import annotations

import json
import re
from typing import Any

_FENCE = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL | re.IGNORECASE)


def extract_json(text: str) -> Any | None:
    if not text:
        return None

    # 1) 优先解析 ```json ... ``` 代码块。
    for match in _FENCE.finditer(text):
        parsed = _try_load(match.group(1))
        if parsed is not None:
            return parsed

    # 2) 直接整体解析。
    parsed = _try_load(text)
    if parsed is not None:
        return parsed

    # 3) 截取第一个 { 或 [ 到对应的最后一个 } 或 ]。
    for open_ch, close_ch in (("{", "}"), ("[", "]")):
        start = text.find(open_ch)
        end = text.rfind(close_ch)
        if 0 <= start < end:
            parsed = _try_load(text[start : end + 1])
            if parsed is not None:
                return parsed
    return None


def _try_load(snippet: str) -> Any | None:
    try:
        return json.loads(snippet.strip())
    except (ValueError, TypeError):
        return None
