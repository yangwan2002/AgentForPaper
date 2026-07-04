"""Token 用量统计。

累计各次 LLM 调用的 prompt/completion token。真实用量优先（API 返回），
缺失时通过注入的 `TokenCounter` 估算并标记 `estimated`，与上下文裁剪、工具
循环使用同一计量口径（Req 7.5）。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from paper_agent.context.tokenizer import TokenCounter, build_token_counter


def estimate_tokens(text: str) -> int:
    """字符数启发式估算（约 2 字符/token），保留以兼容历史调用方。"""
    return max(1, len(text or "") // 2)


@dataclass
class UsageTracker:
    calls: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    estimated: bool = False  # 是否含估算值
    # 统一的 token 计量器；缺少 API 真实计数时用它估算，口径与全局一致。
    counter: TokenCounter = field(default_factory=build_token_counter)

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens

    def add(
        self,
        prompt_text: str,
        completion_text: str,
        prompt_tokens: int | None = None,
        completion_tokens: int | None = None,
    ) -> tuple[int, int]:
        """记录一次调用，返回本次 (prompt, completion) token。

        真实用量（API 返回的 `prompt_tokens` / `completion_tokens`）优先；缺失时
        用注入的 `TokenCounter` 估算并将 `estimated` 置为 True。
        """
        if prompt_tokens is None:
            prompt_tokens = self.counter.count(prompt_text or "")
            self.estimated = True
        if completion_tokens is None:
            completion_tokens = self.counter.count(completion_text or "")
            self.estimated = True
        self.calls += 1
        self.prompt_tokens += prompt_tokens
        self.completion_tokens += completion_tokens
        return prompt_tokens, completion_tokens

    def summary(self) -> str:
        mark = "（含估算）" if self.estimated else ""
        return (
            f"LLM 调用 {self.calls} 次，"
            f"输入 {self.prompt_tokens} + 输出 {self.completion_tokens} "
            f"= 共 {self.total_tokens} tokens{mark}"
        )
