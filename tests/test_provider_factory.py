"""Provider 工厂选择逻辑测试（不触网）。"""

from __future__ import annotations

import pytest

from paper_agent.config import Config
from paper_agent.providers.factory import build_llm_provider, build_retrieval_provider
from paper_agent.providers.llm.mock import MockLLMProvider
from paper_agent.providers.retrieval.mock import MockRetrievalProvider


def test_mock_providers_selected_by_default():
    cfg = Config()
    assert isinstance(build_llm_provider(cfg), MockLLMProvider)
    assert isinstance(build_retrieval_provider(cfg), MockRetrievalProvider)


def test_unknown_provider_raises():
    with pytest.raises(ValueError):
        build_llm_provider(Config(llm_provider="nope"))
    with pytest.raises(ValueError):
        build_retrieval_provider(Config(retrieval_provider="nope"))


def test_preset_vendors_registered():
    """常见 OpenAI 兼容厂商均有预设，接入无需新增代码。"""
    from paper_agent.providers.llm.presets import VENDOR_PRESETS

    for vendor in ("openai", "qwen", "deepseek", "moonshot", "zhipu", "ctrip"):
        assert vendor in VENDOR_PRESETS
        assert VENDOR_PRESETS[vendor].api_key_env
        assert VENDOR_PRESETS[vendor].default_model


def test_custom_vendor_requires_model():
    """自定义端点但未给模型名时应报错。"""
    cfg = Config(llm_provider="custom", llm_base_url="https://x/v1")
    from paper_agent.providers.llm.base import LLMError

    with pytest.raises(LLMError):
        build_llm_provider(cfg)


def test_qwen_requires_api_key(monkeypatch):
    """未配置 DASHSCOPE_API_KEY 时构造 QwenProvider 应报明确错误。"""
    monkeypatch.delenv("DASHSCOPE_API_KEY", raising=False)
    from paper_agent.providers.llm.base import LLMError
    from paper_agent.providers.llm.qwen import QwenProvider

    with pytest.raises(LLMError):
        QwenProvider()


def test_qwen_default_base_url():
    from paper_agent.providers.llm.qwen import DASHSCOPE_BASE_URL

    assert DASHSCOPE_BASE_URL.endswith("/compatible-mode/v1")
