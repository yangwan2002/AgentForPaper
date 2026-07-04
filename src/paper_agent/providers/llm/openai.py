"""OpenAI LLM provider（OpenAI 兼容通用 provider 的预设）。

保留此类以兼容既有调用；实现委托给 OpenAICompatibleProvider。
"""

from __future__ import annotations

from paper_agent.providers.llm.openai_compatible import OpenAICompatibleProvider


class OpenAIProvider(OpenAICompatibleProvider):
    def __init__(
        self,
        model: str = "gpt-4o-mini",
        api_key: str | None = None,
        timeout: float = 60.0,
        base_url: str | None = None,
    ) -> None:
        super().__init__(
            model=model,
            api_key=api_key,
            api_key_env="OPENAI_API_KEY",
            base_url=base_url,
            timeout=timeout,
        )
