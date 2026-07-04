"""终端事件渲染器（零依赖，可选 ANSI 颜色）。

把结构化事件渲染成易读的终端输出，类似 Claude Code / Cursor 的过程展示：
分阶段、分智能体、流式逐字输出模型思考与正文、并显示 token 用量。
"""

from __future__ import annotations

import sys

from paper_agent.observability.events import Event, EventKind


class _C:
    """ANSI 颜色（在不支持时自动留空）。"""

    def __init__(self, enabled: bool) -> None:
        self.dim = "\033[2m" if enabled else ""
        self.cyan = "\033[36m" if enabled else ""
        self.green = "\033[32m" if enabled else ""
        self.yellow = "\033[33m" if enabled else ""
        self.magenta = "\033[35m" if enabled else ""
        self.bold = "\033[1m" if enabled else ""
        self.reset = "\033[0m" if enabled else ""


class ConsoleReporter:
    """实现 EventSink，把事件打印到终端。

    Args:
        show_thinking: 是否流式打印模型思考内容（reasoning）。
        show_llm: 是否打印 LLM 请求预览与流式正文。
        show_usage: 是否打印每次调用的 token 用量。
        preview_chars: 非流式预览/请求预览的最大字符数。
        color: 是否启用 ANSI 颜色。
    """

    def __init__(
        self,
        show_thinking: bool = True,
        show_llm: bool = True,
        show_usage: bool = True,
        preview_chars: int = 300,
        color: bool = True,
    ) -> None:
        self._show_thinking = show_thinking
        self._show_llm = show_llm
        self._show_usage = show_usage
        self._preview = preview_chars
        self._c = _C(color and _supports_color())
        self._streaming = False        # 是否正处于流式输出中
        self._stream_kind = ""         # 当前流的类型（content/thinking）

    def emit(self, event: Event) -> None:
        kind = event.kind
        # 流式增量单独处理（不走整行打印）。
        if kind is EventKind.LLM_DELTA:
            self._on_delta(event)
            return
        # 任何非增量事件到来时，先收尾正在进行的流式行。
        self._end_stream()

        c = self._c
        if kind is EventKind.WORKFLOW_START:
            self._line(f"{c.bold}{c.cyan}▶ 开始任务{c.reset} {event.message}")
        elif kind is EventKind.PHASE:
            self._line(f"\n{c.bold}{c.magenta}■ {event.message}{c.reset}")
        elif kind is EventKind.AGENT_START:
            self._line(f"  {c.cyan}● {event.message}{c.reset}")
        elif kind is EventKind.AGENT_LOG:
            self._line(f"    {c.dim}{event.message}{c.reset}")
        elif kind is EventKind.ITERATION:
            self._line(f"\n{c.bold}{c.yellow}↻ {event.message}{c.reset}")
        elif kind is EventKind.REVIEW_SCORES:
            self._line(f"  {c.green}✓ {event.message}{c.reset}")
        elif kind is EventKind.LLM_REQUEST:
            if self._show_llm:
                self._line(f"    {c.dim}↗ 请求：{self._cut(event.message)}{c.reset}")
        elif kind is EventKind.LLM_THINKING:
            if self._show_thinking:
                self._line(f"    {c.dim}🤔 思考：{self._cut(event.message)}{c.reset}")
        elif kind is EventKind.LLM_RESPONSE:
            if self._show_llm:
                self._line(f"    {c.dim}↙ 响应：{self._cut(event.message)}{c.reset}")
        elif kind is EventKind.LLM_USAGE:
            if self._show_usage:
                self._line(f"    {c.dim}🔢 {event.message}{c.reset}")
        elif kind is EventKind.DEGRADATION:
            feature = event.data.get("feature", "")
            reason = event.data.get("reason", "")
            venue_id = event.data.get("venue_id", "")
            detail = " ".join(
                part
                for part in (
                    f"feature={feature}" if feature else "",
                    f"reason={reason}" if reason else "",
                    f"venue_id={venue_id}" if venue_id else "",
                )
                if part
            )
            suffix = f" {c.dim}[{detail}]{c.reset}" if detail else ""
            self._line(f"  {c.yellow}⚠ 降级：{self._cut(event.message)}{c.reset}{suffix}")
        elif kind is EventKind.EXPORT_ASSET:
            self._line(f"    {c.dim}💾 资产：{self._cut(event.message)}{c.reset}")
        elif kind is EventKind.WORKFLOW_END:
            self._line(f"\n{c.bold}{c.green}✔ 完成{c.reset} {event.message}")

    # --- 流式增量 ---

    def _on_delta(self, event: Event) -> None:
        delta_kind = event.data.get("kind", "content")
        if delta_kind == "thinking" and not self._show_thinking:
            return
        if delta_kind == "content" and not self._show_llm:
            return
        c = self._c
        if not self._streaming or self._stream_kind != delta_kind:
            # 切换流类型或开始新流：先收尾旧流，打印前缀。
            self._end_stream()
            prefix = "    🤔 " if delta_kind == "thinking" else "    ↙ "
            sys.stdout.write(f"{c.dim}{prefix}{c.reset}")
            self._streaming = True
            self._stream_kind = delta_kind
        text = event.message
        if delta_kind == "thinking":
            sys.stdout.write(f"{c.dim}{text}{c.reset}")
        else:
            sys.stdout.write(text)
        sys.stdout.flush()

    def _end_stream(self) -> None:
        if self._streaming:
            sys.stdout.write("\n")
            sys.stdout.flush()
            self._streaming = False
            self._stream_kind = ""

    def _cut(self, text: str) -> str:
        text = " ".join((text or "").split())
        return text if len(text) <= self._preview else text[: self._preview] + " …"

    @staticmethod
    def _line(text: str) -> None:
        print(text, file=sys.stdout, flush=True)


def _supports_color() -> bool:
    return hasattr(sys.stdout, "isatty") and sys.stdout.isatty()
