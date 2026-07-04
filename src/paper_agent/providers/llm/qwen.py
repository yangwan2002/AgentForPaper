"""通义千问（DashScope）LLM provider —— OpenAI 兼容通用 provider 的预设。

千问提供 OpenAI 兼容接口，故仅需指定 DashScope 端点与 DASHSCOPE_API_KEY。
默认端点为国内（北京）地域；海外地域可通过 base_url 覆盖：
- 国内（北京）：https://dashscope.aliyuncs.com/compatible-mode/v1
- 美国（弗吉尼亚）：https://dashscope-us.aliyuncs.com/compatible-mode/v1

常用模型：qwen-plus、qwen-turbo、qwen-max。
端点信息据阿里云百炼（Model Studio）文档，内容经改写以符合合规要求。
"""

from __future__ import annotations

from paper_agent.providers.llm.openai_compatible import OpenAICompatibleProvider

DASHSCOPE_BASE_URL = "https://dashscope.aliyuncs.com/compatible-mode/v1"


class QwenProvider(OpenAICompatibleProvider):
    def __init__(
        self,
        model: str = "qwen-plus",
        api_key: str | None = None,
        timeout: float = 60.0,
        base_url: str | None = None,
    ) -> None:
        super().__init__(
            model=model,
            api_key=api_key,
            api_key_env="DASHSCOPE_API_KEY",
            base_url=base_url or DASHSCOPE_BASE_URL,
            timeout=timeout,
        )
