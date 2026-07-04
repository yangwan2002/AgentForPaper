"""保结构原地润色共享核心单测（inplace_core）。"""

from __future__ import annotations

from paper_agent.inplace_core import InplaceStats, ProsePolishGuard, polish_fragment
from paper_agent.providers.llm.base import LLMResponse


class _LLM:
    def __init__(self, reply):
        self._reply = reply

    def complete(self, messages, **opts):
        return LLMResponse(content=self._reply)


class _BoomLLM:
    def complete(self, messages, **opts):
        raise RuntimeError("llm down")


def _prompt(core):
    from paper_agent.providers.llm.base import Message
    return [Message(role="user", content=core)]


def test_guard_rejects_empty_candidate():
    guard = ProsePolishGuard([lambda o, c: True])
    assert guard.passes("orig", "   ") is False
    assert guard.passes("orig", "new") is True


def test_guard_requires_all_checks():
    guard = ProsePolishGuard([lambda o, c: True, lambda o, c: False])
    assert guard.passes("o", "c") is False


def test_polish_fragment_returns_candidate_when_guard_passes():
    guard = ProsePolishGuard([lambda o, c: True])
    out = polish_fragment(_LLM("polished"), _prompt, "original text", guard)
    assert out == "polished"


def test_polish_fragment_returns_none_when_guard_fails():
    guard = ProsePolishGuard([lambda o, c: False])
    out = polish_fragment(_LLM("polished"), _prompt, "original text", guard)
    assert out is None


def test_polish_fragment_swallows_llm_error():
    guard = ProsePolishGuard([lambda o, c: True])
    assert polish_fragment(_BoomLLM(), _prompt, "orig", guard) is None


def test_polish_fragment_preserves_edges():
    guard = ProsePolishGuard([lambda o, c: True])
    out = polish_fragment(_LLM("CORE"), _prompt, "  \n text \n ", guard, preserve_edges=True)
    # 前后空白（缩进/换行）被原样保留，只替换核心。
    assert out == "  \n CORE \n "


def test_polish_fragment_no_preserve_edges_sends_whole():
    seen = {}

    class _Rec:
        def complete(self, messages, **opts):
            seen["sent"] = messages[0].content
            return LLMResponse(content="ok")

    guard = ProsePolishGuard([lambda o, c: True])
    polish_fragment(_Rec(), _prompt, "  spaced  ", guard, preserve_edges=False)
    assert seen["sent"] == "  spaced  "  # 整段（含空白）送 LLM


def test_stats_defaults():
    s = InplaceStats()
    assert (s.total, s.polished, s.rejected) == (0, 0, 0)
