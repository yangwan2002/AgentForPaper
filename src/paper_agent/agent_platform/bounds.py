"""有界性检查（从 Orchestrator 抽取为共享纯函数）。

顶层 Agent_Loop 与既有 Orchestrator 反馈循环共用「墙钟超时」「全局 token 预算」
两类有界判定，抽为无副作用纯函数，便于复用与单测（Req 9.1/9.2）。
"""

from __future__ import annotations

import time


def deadline_exceeded(start_time: float, limit_s: float) -> bool:
    """墙钟是否超时。``limit_s <= 0`` 表示不限（恒 False）。"""
    if not limit_s or limit_s <= 0:
        return False
    return (time.monotonic() - start_time) >= limit_s


def budget_exceeded(total_tokens: int, cap: int) -> bool:
    """全局 token 用量是否达/超预算。``cap <= 0`` 表示不限（恒 False）。"""
    if not cap or cap <= 0:
        return False
    return total_tokens >= cap


__all__ = ["deadline_exceeded", "budget_exceeded"]
