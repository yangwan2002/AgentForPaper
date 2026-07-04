"""AnthropicProvider 适配器单元测试。

用注入的 fake 客户端验证协议转换（无需 anthropic SDK / 网络）：
- system 抽离、tool_result 合并进 user 消息；
- OpenAI tools schema → Anthropic input_schema；
- content block（text/thinking/tool_use）→ 中立 LLMResponse。
"""

from __future__ import annotations

from types import SimpleNamespace

from paper_agent.providers.llm.anthropic_provider import (
    AnthropicProvider,
    _to_anthropic_messages,
    _to_anthropic_tools,
)
from paper_agent.providers.llm.base import Message, ToolCall


class _FakeClient:
    """记录最后一次 create 入参，返回预置的 Anthropic 风格响应。"""

    def __init__(self, response):
        self._response = response
        self.last_kwargs = None
        self.messages = SimpleNamespace(create=self._create)

    def _create(self, **kwargs):
        self.last_kwargs = kwargs
        return self._response


def _block(**kw):
    return SimpleNamespace(**kw)


# --- 协议转换：消息 ----------------------------------------------------------

def test_system_extracted_and_tool_results_merged():
    messages = [
        Message(role="system", content="你是助手"),
        Message(role="user", content="改引言"),
        Message(role="assistant", content="", tool_calls=[
            ToolCall(id="t1", name="rewrite", arguments={"x": 1}),
            ToolCall(id="t2", name="read", arguments={}),
        ]),
        Message(role="tool", content="结果1", tool_call_id="t1"),
        Message(role="tool", content="结果2", tool_call_id="t2"),
        Message(role="user", content="继续"),
    ]
    system, conv = _to_anthropic_messages(messages)
    assert system == "你是助手"
    # user, assistant(2 blocks), user(tool_result x2 合并), user
    assert conv[0] == {"role": "user", "content": "改引言"}
    assert conv[1]["role"] == "assistant"
    assert [b["type"] for b in conv[1]["content"]] == ["tool_use", "tool_use"]
    # 两个 tool_result 合并进同一条 user 消息。
    assert conv[2]["role"] == "user"
    assert [b["type"] for b in conv[2]["content"]] == ["tool_result", "tool_result"]
    assert conv[2]["content"][0]["tool_use_id"] == "t1"
    assert conv[3] == {"role": "user", "content": "继续"}


def test_assistant_text_and_tool_use_blocks():
    messages = [
        Message(role="assistant", content="思考后调用", tool_calls=[
            ToolCall(id="t1", name="foo", arguments={"a": "b"})]),
    ]
    _, conv = _to_anthropic_messages(messages)
    blocks = conv[0]["content"]
    assert blocks[0] == {"type": "text", "text": "思考后调用"}
    assert blocks[1] == {"type": "tool_use", "id": "t1", "name": "foo", "input": {"a": "b"}}


# --- 协议转换：tools schema --------------------------------------------------

def test_openai_tools_to_anthropic_schema():
    openai_tools = [{
        "type": "function",
        "function": {
            "name": "rewrite_section",
            "description": "改写",
            "parameters": {"type": "object", "properties": {"section_id": {"type": "string"}}},
        },
    }]
    out = _to_anthropic_tools(openai_tools)
    assert out == [{
        "name": "rewrite_section",
        "description": "改写",
        "input_schema": {"type": "object", "properties": {"section_id": {"type": "string"}}},
    }]


# --- 响应解析 ----------------------------------------------------------------

def test_complete_parses_text_thinking_and_tool_use():
    response = SimpleNamespace(
        content=[
            _block(type="thinking", thinking="先想想"),
            _block(type="text", text="这是回答"),
            _block(type="tool_use", id="tu1", name="locate_section", input={"reference": "实验"}),
        ],
        usage=SimpleNamespace(input_tokens=11, output_tokens=22),
    )
    client = _FakeClient(response)
    provider = AnthropicProvider(model="qwen3.7-max", api_key="k", client=client)
    resp = provider.complete(
        [Message(role="system", content="sys"), Message(role="user", content="hi")],
        tools=[{"type": "function", "function": {"name": "locate_section", "description": "d", "parameters": {}}}],
    )
    assert resp.content == "这是回答"
    assert resp.reasoning == "先想想"
    assert resp.prompt_tokens == 11 and resp.completion_tokens == 22
    assert len(resp.tool_calls) == 1
    assert resp.tool_calls[0].name == "locate_section"
    assert resp.tool_calls[0].arguments == {"reference": "实验"}
    # 校验下发给网关的入参：system 抽离、tools 转 input_schema、max_tokens 必填。
    kw = client.last_kwargs
    assert kw["system"] == "sys"
    assert kw["model"] == "qwen3.7-max"
    assert "max_tokens" in kw
    assert kw["tools"][0]["name"] == "locate_section"
    assert "input_schema" in kw["tools"][0]


def test_complete_no_tools_no_system():
    response = SimpleNamespace(
        content=[_block(type="text", text="纯文本")],
        usage=SimpleNamespace(input_tokens=1, output_tokens=2),
    )
    client = _FakeClient(response)
    provider = AnthropicProvider(model="m", api_key="k", client=client)
    resp = provider.complete([Message(role="user", content="hi")])
    assert resp.content == "纯文本"
    assert resp.tool_calls == []
    assert "system" not in client.last_kwargs
    assert "tools" not in client.last_kwargs


def test_stream_yields_content_chunk():
    response = SimpleNamespace(
        content=[_block(type="text", text="流式正文")],
        usage=SimpleNamespace(input_tokens=1, output_tokens=1),
    )
    provider = AnthropicProvider(model="m", api_key="k", client=_FakeClient(response))
    chunks = list(provider.stream([Message(role="user", content="hi")]))
    assert any(c.kind == "content" and c.text == "流式正文" for c in chunks)


# --- on_delta 流式补全 -------------------------------------------------------

class _StreamCtx:
    """模拟 anthropic 的 messages.stream(...) 上下文管理器。"""

    def __init__(self, deltas, final):
        self.text_stream = iter(deltas)
        self._final = final

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def get_final_message(self):
        return self._final


class _StreamingClient:
    def __init__(self, deltas, final):
        self._deltas = deltas
        self._final = final
        self.messages = SimpleNamespace(stream=self._stream, create=self._create)

    def _stream(self, **kwargs):
        return _StreamCtx(self._deltas, self._final)

    def _create(self, **kwargs):
        return self._final


def test_complete_with_on_delta_streams_content():
    final = SimpleNamespace(
        content=[_block(type="text", text="你好世界"),
                 _block(type="tool_use", id="t1", name="foo", input={"a": 1})],
        usage=SimpleNamespace(input_tokens=3, output_tokens=4),
    )
    client = _StreamingClient(deltas=["你好", "世界"], final=final)
    provider = AnthropicProvider(model="m", api_key="k", client=client)

    seen = []
    resp = provider.complete(
        [Message(role="user", content="hi")],
        on_delta=lambda kind, text: seen.append((kind, text)),
    )
    # 增量按序推送。
    assert seen == [("content", "你好"), ("content", "世界")]
    # 最终响应含完整正文与工具调用（流式仍能拿到 tool_use）。
    assert resp.content == "你好世界"
    assert len(resp.tool_calls) == 1 and resp.tool_calls[0].name == "foo"


def test_complete_on_delta_falls_back_when_stream_unsupported():
    final = SimpleNamespace(
        content=[_block(type="text", text="回退正文")],
        usage=SimpleNamespace(input_tokens=1, output_tokens=1),
    )

    class _NoStreamClient:
        def __init__(self):
            self.messages = SimpleNamespace(stream=self._boom, create=lambda **k: final)

        def _boom(self, **kwargs):
            raise RuntimeError("网关不支持流式")

    seen = []
    provider = AnthropicProvider(model="m", api_key="k", client=_NoStreamClient())
    resp = provider.complete(
        [Message(role="user", content="hi")],
        on_delta=lambda kind, text: seen.append((kind, text)),
    )
    # 流式失败 → 回退非流式，并把完整正文补推一次。
    assert resp.content == "回退正文"
    assert seen == [("content", "回退正文")]


# --- 工厂接线 ----------------------------------------------------------------

def test_factory_builds_anthropic_provider(monkeypatch):
    from paper_agent.config import Config
    from paper_agent.providers.factory import build_llm_provider

    monkeypatch.setenv("MY_ANTHROPIC_KEY", "secret")
    config = Config(
        llm_provider="anthropic",
        llm_model="qwen3.7-max",
        llm_base_url="https://gateway.example/anthropic",
        llm_api_key_env="MY_ANTHROPIC_KEY",
    )
    provider = build_llm_provider(config)
    assert isinstance(provider, AnthropicProvider)
