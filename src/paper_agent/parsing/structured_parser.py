"""结构化输出统一治理组件（升级 Req 3）。

`StructuredParser` 把「调用 LLM → 解析 JSON → 失败处理」的散落逻辑收敛到一处，
明确区分三类来源状态（见 `ParseStatus`）：

- ``PARSED``：成功解析为合法结构，且 ``data`` 完全来源于 provider 实际返回。
- ``MOCK_FALLBACK``：识别为 Mock/测试 provider 的非结构化输出，解析失败时的回退。
- ``FAILED``：生产 provider 多次强约束重试后仍无法解析，**不返回任何 data**。

核心不变量（Req 3.7）：任何情况下都**不**返回带占位、默认或合成内容的
``PARSED`` 结果——``PARSED`` 的 ``data`` 必须逐字来自 provider 返回的文本。
"""

from __future__ import annotations

from dataclasses import dataclass

from paper_agent.providers.llm.base import LLMProvider, Message
from paper_agent.utils.json_parse import extract_json
from paper_agent.workspace.models import ParseStatus

# JSON 模式请求参数（OpenAI 兼容）。provider 不支持时调用会抛错，由本组件回退。
_JSON_MODE_OPT: dict = {"response_format": {"type": "json_object"}}


@dataclass
class ParseOutcome:
    """一次结构化解析的结果。

    - ``status``：来源状态（见 ``ParseStatus``）。
    - ``data``：仅当 ``status == PARSED`` 时为 provider 实际返回的字典；
      ``MOCK_FALLBACK`` / ``FAILED`` 时恒为 ``None``（Req 3.8）。
    - ``raw``：最近一次 provider 返回的原始文本（用于诊断/预览）。
    - ``attempts``：实际调用 LLM 的次数。
    - ``reason``：解析失败原因类别（``PARSED`` 时为空字符串）。
    """

    status: ParseStatus
    data: dict | None = None
    raw: str = ""
    attempts: int = 0
    reason: str = ""


class StructuredParser:
    """统一的结构化（JSON）输出解析器。

    Args:
        llm: 注入的 LLM provider 抽象（可为任意装饰层叠后的 provider）。
        max_parse_retries: 生产解析失败时的最大额外重试次数（Req 3.6，范围 0–5）。
            实际生产路径最多调用 LLM ``max_parse_retries + 1`` 次。
    """

    def __init__(
        self, llm: LLMProvider, max_parse_retries: int = 1, *, is_mock: bool = False
    ) -> None:
        self._llm = llm
        # 约束到 [0, 5]（Req 3.6）。
        self._max_parse_retries = max(0, min(5, max_parse_retries))
        # provider 是否支持 JSON 模式：首次调用失败后置为 False 以避免重复双调用。
        self._json_mode_supported = True
        # #12：is_mock 作为解析器实例属性，由装配期一次性注入；调用方无需在每次
        # request_json 时重复传递（业务智能体不再感知 mock 概念）。仍保留 per-call
        # 覆盖参数以兼容既有测试。
        self._is_mock = is_mock

    def request_json(
        self,
        messages: list[Message],
        *,
        required_keys: tuple[str, ...] = (),
        is_mock: bool | None = None,
    ) -> ParseOutcome:
        """调用 LLM 并解析为 JSON 字典。

        Preconditions: ``messages`` 非空。
        Postconditions:
            - ``status == PARSED`` ⟹ ``data`` 为 dict 且含全部 ``required_keys`` 且各值非空，
              且 ``data`` 完全来源于 provider 实际返回（Req 3.3 / 3.7）。
            - ``status == MOCK_FALLBACK`` ⟹ ``is_mock == True``（Req 3.5）。
            - ``status == FAILED`` ⟹ ``is_mock == False`` 且已尝试
              ``max_parse_retries + 1`` 次，且 ``data is None``（Req 3.6 / 3.8）。

        #12：``is_mock`` 缺省（``None``）时取实例属性；显式传入则覆盖（兼容旧测试）。
        """
        # 取本调用生效的 is_mock：显式传入优先，否则用实例属性。
        effective_is_mock = self._is_mock if is_mock is None else is_mock
        attempts = 0
        last_raw = ""
        last_reason = "empty_output"
        # Mock 仅尝试一次即回退；生产按强约束重试至上限。
        max_attempts = 1 if effective_is_mock else self._max_parse_retries + 1
        current_messages = messages

        for _ in range(max_attempts):
            attempts += 1
            raw = self._call_llm(current_messages)
            last_raw = raw

            data, reason = self._parse(raw, required_keys)
            if data is not None:
                return ParseOutcome(
                    status=ParseStatus.PARSED,
                    data=data,
                    raw=raw,
                    attempts=attempts,
                )

            last_reason = reason

            if effective_is_mock:
                # 测试/Mock provider：解析失败走确定性回退语义（Req 3.5）。
                return ParseOutcome(
                    status=ParseStatus.MOCK_FALLBACK,
                    data=None,
                    raw=raw,
                    attempts=attempts,
                    reason=reason,
                )

            # 生产路径：以强约束提示准备下一次重试（Req 3.6）。
            current_messages = self._with_constraint(
                messages, required_keys, reason
            )

        # 生产路径耗尽重试仍失败：显式失败，不返回 data（Req 3.6 / 3.8）。
        return ParseOutcome(
            status=ParseStatus.FAILED,
            data=None,
            raw=last_raw,
            attempts=attempts,
            reason=last_reason,
        )

    # --- 内部辅助 ---

    def _call_llm(self, messages: list[Message]) -> str:
        """调用 LLM，优先启用 JSON 模式（Req 3.1）。

        provider 不支持 ``response_format`` 时调用会抛错，此处回退到普通调用
        （Req 3.2）；回退后记住该 provider 不支持 JSON 模式，避免重复双调用。

        #17：生产装配下本解析器经 ``ResilientLLMProvider`` 调底层——瞬时错误
        （429/5xx/连接重置）已被 Resilient 重试吸收，不会冒泡到此处；故
        ``response_format`` 抛错几乎只会在「provider 真不支持 JSON 模式」
        （非重试的 400 BadRequest）时发生，此时永久禁用是正确行为。
        """
        if self._json_mode_supported:
            try:
                resp = self._llm.complete(messages, **_JSON_MODE_OPT)
                return resp.content or ""
            except Exception:  # noqa: BLE001 - provider 可能不支持 JSON 模式
                # 不支持 JSON 模式：回退普通调用并对后续请求禁用 JSON 模式。
                self._json_mode_supported = False
        resp = self._llm.complete(messages)
        return resp.content or ""

    def _parse(
        self, raw: str, required_keys: tuple[str, ...]
    ) -> tuple[dict | None, str]:
        """把原始文本解析为满足约束的字典。

        返回 ``(data, reason)``：解析成功时 ``data`` 为字典、``reason`` 为空；
        失败时 ``data`` 为 ``None``、``reason`` 标识失败类别。
        """
        if not raw or not raw.strip():
            return None, "empty_output"

        data = extract_json(raw)
        if not isinstance(data, dict):
            return None, "not_a_json_object"

        missing = [k for k in required_keys if k not in data]
        if missing:
            return None, "missing_required_keys:" + ",".join(missing)

        empty = [k for k in required_keys if self._is_empty(data[k])]
        if empty:
            return None, "empty_required_values:" + ",".join(empty)

        return data, ""

    @staticmethod
    def _is_empty(value) -> bool:
        """判定键值是否为「空」：None、空白字符串或空容器视为空。

        数值 0 与布尔 False 是有效取值，不视为空。
        """
        if value is None:
            return True
        if isinstance(value, str):
            return not value.strip()
        if isinstance(value, (list, dict, tuple, set)):
            return len(value) == 0
        return False

    @staticmethod
    def _with_constraint(
        messages: list[Message],
        required_keys: tuple[str, ...],
        reason: str,
    ) -> list[Message]:
        """在原消息后追加一条强约束提示，引导模型重试产出合法 JSON。"""
        keys_hint = "、".join(required_keys) if required_keys else "全部要求的字段"
        instruction = (
            f"上一次输出无法被解析为合法 JSON（原因：{reason}）。"
            "请只输出一个合法的 JSON 对象，不要包含任何额外解释、Markdown 代码块"
            f"或前后缀文本。该 JSON 对象必须包含以下非空字段：{keys_hint}。"
        )
        return list(messages) + [Message(role="user", content=instruction)]
