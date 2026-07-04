"""保结构原地润色的共享核心（被 latex_inplace / docx_inplace 复用）。

两种格式的 in-place 润色（LaTeX 源、DOCX）**范式相同**：确定性地定位"可改的散文单元"→
只把散文喂 LLM → 确定性守卫校验（引用/数字恒等等）→ 通过才替换、否则保留原文。差异只在
"如何遍历散文单元、如何写回、如何做结构完整性兜底"（各格式自留）。

本模块抽取**格式无关**的两块共性，消除重复、统一行为：
- :class:`ProsePolishGuard`：把若干 ``(original, candidate) -> bool`` 守卫组合成一个判定；
  空候选直接判负。
- :func:`polish_fragment`：统一"调 LLM → strip → 守卫 → 返回润色文本或 None（保留原文）"，
  可选保留片段前后空白（缩进/换行属版式，LaTeX 需要）。
- :class:`InplaceStats`：统一的润色计数（总/已润色/被守卫拦截）。

设计不变式：``polish_fragment`` 在 LLM 异常或守卫未过时返回 ``None``，调用方保留原文——
"宁可少改也不破坏"。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from paper_agent.providers.llm.base import LLMProvider, Message


@dataclass
class InplaceStats:
    """原地润色计数。"""

    total: int = 0        # 值得润色的散文单元数
    polished: int = 0     # 实际被润色替换的数
    rejected: int = 0     # 因守卫拦截/LLM 失败而保留原文的数


class ProsePolishGuard:
    """把若干确定性守卫组合成一个判定：空候选直接判负，其余需全部通过。

    每个 check 形如 ``(original, candidate) -> bool``（如引用集合恒等、数字多重集合恒等、
    LaTeX 结构保持、长度浮动受限）。
    """

    def __init__(self, checks: list[Callable[[str, str], bool]]) -> None:
        self._checks = list(checks)

    def passes(self, original: str, candidate: str) -> bool:
        if not candidate.strip():
            return False
        return all(check(original, candidate) for check in self._checks)


def polish_fragment(
    llm: LLMProvider,
    prompt_fn: Callable[[str], list[Message]],
    original: str,
    guard: ProsePolishGuard,
    *,
    preserve_edges: bool = False,
) -> str | None:
    """润色单个散文片段：通过守卫返回润色文本，否则返回 ``None``（调用方保留原文）。

    - ``prompt_fn``：把（strip 后的）片段文本构造成 LLM 消息（各格式用不同 template）。
    - ``preserve_edges``：为 True 时只把 strip 后的核心送 LLM，再用原前后空白包裹润色结果
      （保留缩进/换行等版式，LaTeX 用）；为 False 时整段送 LLM（DOCX 段落用）。

    LLM 抛异常 → 返回 ``None``（不阻断，保留原文）。
    """
    if preserve_edges:
        leading = original[: len(original) - len(original.lstrip())]
        trailing = original[len(original.rstrip()):]
        core = original.strip()
    else:
        leading = trailing = ""
        core = original
    if not core.strip():
        return None
    try:
        resp = llm.complete(prompt_fn(core))
    except Exception:  # noqa: BLE001 - 润色失败不阻断，保留原文
        return None
    candidate = (resp.content or "").strip()
    if not candidate or not guard.passes(core, candidate):
        return None
    return f"{leading}{candidate}{trailing}"


__all__ = ["InplaceStats", "ProsePolishGuard", "polish_fragment"]
