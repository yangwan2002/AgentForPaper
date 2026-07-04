"""`StructuredParser` 单元测试（升级 Req 3.2–3.8）。

覆盖六条路径：
(a) JSON 模式成功            → PARSED
(b) 不支持 JSON 模式时降级抽取成功 → PARSED
(c) 缺 required_keys         → FAILED（is_mock=False）
(d) 空键值                   → FAILED（is_mock=False）
(e) Mock 不可解析输出回退     → MOCK_FALLBACK（is_mock=True）
(f) 生产不可解析重试至上限    → FAILED，data is None、reason 非空、attempts==max+1

测试使用轻量级 fake provider 驱动各路径，零网络依赖。
"""

from __future__ import annotations

import pytest

from paper_agent.parsing.structured_parser import ParseOutcome, StructuredParser
from paper_agent.providers.llm.base import LLMResponse, Message
from paper_agent.workspace.models import ParseStatus


# --- 轻量级 fake provider ---


class FixedProvider:
    """对任意调用恒定返回同一段文本，并记录每次调用的 opts。"""

    def __init__(self, content: str) -> None:
        self._content = content
        self.calls: list[dict] = []

    def complete(self, messages: list[Message], **opts) -> LLMResponse:
        self.calls.append(opts)
        return LLMResponse(content=self._content)


class NoJsonModeProvider:
    """模拟不支持 `response_format` 的 provider。

    当调用携带 `response_format` 时抛错（触发 StructuredParser 的降级），
    普通调用则返回给定文本。记录普通调用次数以便断言降级生效。
    """

    def __init__(self, content: str) -> None:
        self._content = content
        self.plain_calls = 0
        self.json_mode_calls = 0

    def complete(self, messages: list[Message], **opts) -> LLMResponse:
        if "response_format" in opts:
            self.json_mode_calls += 1
            raise TypeError("provider 不支持 response_format")
        self.plain_calls += 1
        return LLMResponse(content=self._content)


def _msgs() -> list[Message]:
    return [Message(role="user", content="请输出 JSON")]


# --- (a) JSON 模式成功 → PARSED（Req 3.3） ---


def test_json_mode_success_returns_parsed_with_provider_data():
    provider = FixedProvider('{"title": "T", "body": "B"}')
    parser = StructuredParser(provider, max_parse_retries=1)

    outcome = parser.request_json(_msgs(), required_keys=("title", "body"))

    assert outcome.status == ParseStatus.PARSED
    # data 完全来源于 provider 实际返回（Req 3.3 / 3.7）。
    assert outcome.data == {"title": "T", "body": "B"}
    assert outcome.attempts == 1
    assert outcome.reason == ""
    # 首次应启用 JSON 模式（response_format）。
    assert provider.calls[0].get("response_format") == {"type": "json_object"}


# --- (b) 不支持 JSON 模式时降级抽取成功 → PARSED（Req 3.2） ---


def test_fallback_extract_json_success_returns_parsed():
    # provider 把 JSON 包在 ```json 代码块里，且拒绝 response_format。
    raw = "这是说明\n```json\n{\"k\": \"v\"}\n```\n结尾"
    provider = NoJsonModeProvider(raw)
    parser = StructuredParser(provider, max_parse_retries=1)

    outcome = parser.request_json(_msgs(), required_keys=("k",))

    assert outcome.status == ParseStatus.PARSED
    assert outcome.data == {"k": "v"}
    assert outcome.attempts == 1
    # 降级生效：JSON 模式尝试过一次并失败，随后走普通调用成功。
    assert provider.json_mode_calls == 1
    assert provider.plain_calls == 1


def test_fallback_disables_json_mode_for_subsequent_calls():
    # 一旦探测到不支持 JSON 模式，后续不应再重复双调用。
    provider = NoJsonModeProvider("not json at all")
    parser = StructuredParser(provider, max_parse_retries=2)

    outcome = parser.request_json(_msgs(), required_keys=("k",))

    assert outcome.status == ParseStatus.FAILED
    # 仅首次尝试 JSON 模式一次，之后所有重试都走普通调用。
    assert provider.json_mode_calls == 1
    assert provider.plain_calls == outcome.attempts


# --- (c) 缺 required_keys → FAILED（Req 3.4 / 3.6 / 3.8） ---


def test_missing_required_keys_returns_failed():
    provider = FixedProvider('{"title": "only title"}')
    parser = StructuredParser(provider, max_parse_retries=1)

    outcome = parser.request_json(_msgs(), required_keys=("title", "body"))

    assert outcome.status == ParseStatus.FAILED
    assert outcome.data is None  # FAILED 不返回 data（Req 3.8）。
    assert "missing_required_keys" in outcome.reason
    assert "body" in outcome.reason
    # 生产路径重试至上限：max_parse_retries + 1 次。
    assert outcome.attempts == 2


# --- (d) 空键值 → FAILED（Req 3.4 / 3.8） ---


def test_empty_required_value_returns_failed():
    provider = FixedProvider('{"title": "ok", "body": "   "}')
    parser = StructuredParser(provider, max_parse_retries=0)

    outcome = parser.request_json(_msgs(), required_keys=("title", "body"))

    assert outcome.status == ParseStatus.FAILED
    assert outcome.data is None
    assert "empty_required_values" in outcome.reason
    assert "body" in outcome.reason
    # max_parse_retries=0 → 仅尝试一次。
    assert outcome.attempts == 1


def test_empty_required_value_empty_list_returns_failed():
    provider = FixedProvider('{"items": []}')
    parser = StructuredParser(provider, max_parse_retries=0)

    outcome = parser.request_json(_msgs(), required_keys=("items",))

    assert outcome.status == ParseStatus.FAILED
    assert outcome.data is None
    assert "empty_required_values" in outcome.reason


def test_zero_and_false_are_valid_non_empty_values():
    # 数值 0 与布尔 False 是有效取值，不应被判为空（Req 3.3）。
    provider = FixedProvider('{"count": 0, "flag": false}')
    parser = StructuredParser(provider, max_parse_retries=0)

    outcome = parser.request_json(_msgs(), required_keys=("count", "flag"))

    assert outcome.status == ParseStatus.PARSED
    assert outcome.data == {"count": 0, "flag": False}


# --- (e) Mock 不可解析输出 → MOCK_FALLBACK（Req 3.5） ---


def test_mock_unparsable_output_returns_mock_fallback():
    provider = FixedProvider("[mock] 这是一段无法解析为 JSON 的纯文本")
    parser = StructuredParser(provider, max_parse_retries=3)

    outcome = parser.request_json(
        _msgs(), required_keys=("title",), is_mock=True
    )

    assert outcome.status == ParseStatus.MOCK_FALLBACK
    assert outcome.data is None  # 非 PARSED 不返回 data。
    assert outcome.reason != ""
    # Mock 仅尝试一次即回退，不重试。
    assert outcome.attempts == 1


# --- (f) 生产不可解析重试至上限 → FAILED（Req 3.6 / 3.8） ---


@pytest.mark.parametrize("max_retries", [0, 1, 3])
def test_production_unparsable_exhausts_retries_then_failed(max_retries):
    provider = FixedProvider("完全不是 JSON 的输出")
    parser = StructuredParser(provider, max_parse_retries=max_retries)

    outcome = parser.request_json(_msgs(), required_keys=("title",))

    assert isinstance(outcome, ParseOutcome)
    assert outcome.status == ParseStatus.FAILED
    assert outcome.data is None  # Req 3.8。
    assert outcome.reason != ""  # 附失败原因。
    # 生产路径调用 LLM 次数 == max_parse_retries + 1（Req 3.6）。
    assert outcome.attempts == max_retries + 1


def test_max_parse_retries_clamped_to_upper_bound():
    # max_parse_retries 超出上限 5 时被钳制为 5（Req 3.6）。
    provider = FixedProvider("not json")
    parser = StructuredParser(provider, max_parse_retries=99)

    outcome = parser.request_json(_msgs(), required_keys=("k",))

    assert outcome.status == ParseStatus.FAILED
    assert outcome.attempts == 6  # 5 次重试 + 1 次初始。


def test_empty_output_returns_failed_with_reason():
    provider = FixedProvider("")
    parser = StructuredParser(provider, max_parse_retries=0)

    outcome = parser.request_json(_msgs(), required_keys=("k",))

    assert outcome.status == ParseStatus.FAILED
    assert outcome.reason == "empty_output"
    assert outcome.data is None
