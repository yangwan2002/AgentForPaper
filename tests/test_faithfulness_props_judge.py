"""Property-based tests for citation-faithfulness-audit · FaithfulnessJudge 判定层。

- Property 8: PARSED 采用合法枚举，非法值降级（Req 3.3）——当注入的 parser 返回
  ``PARSED`` 且 ``data['verdict']`` 为任意字符串时，``judge`` 的 verdict 恰为
  ``FaithfulnessVerdict(s)``（合法枚举原样，非法值经 ``_missing_`` 降级 cannot_verify）。
- Property 9: 非 PARSED 或异常绝不 supported（核心安全属性，Req 3.4/3.5/3.6/7.1）——
  当 parser 返回 FAILED / MOCK_FALLBACK 或 ``request_json`` 抛异常时，``judge``
  永不返回 ``supported`` / ``weak_support``，一律降级 ``cannot_verify``。
- Property 10: verdict 全域属于枚举且严重度映射为全函数（Req 4.1–4.5）——
  ``severity_for`` 对每个枚举成员返回 {high, medium, low, none} 中的确定值，
  且 ``judge`` 返回的 verdict 恒为枚举成员。

生成器约束（对齐既有 props 测试）：任意 ``st.text`` 一律排除 unicode 代理区 "Cs"
与控制字符 "Cc"。
"""

from __future__ import annotations

from hypothesis import given, settings
from hypothesis import strategies as st

from paper_agent.agents.citation_faithfulness_agent import (
    FaithfulnessJudge,
    severity_for,
)
from paper_agent.parsing.structured_parser import ParseOutcome
from paper_agent.workspace.faithfulness import FaithfulnessVerdict
from paper_agent.workspace.models import ParseStatus

# --------------------------------------------------------------------------- #
# Stub parser
# --------------------------------------------------------------------------- #

_VALID_VERDICTS = {v.value for v in FaithfulnessVerdict}
_SUPPORTED_LIKE = {FaithfulnessVerdict.SUPPORTED, FaithfulnessVerdict.WEAK_SUPPORT}

# 合法/垃圾 verdict 字符串：覆盖枚举值与任意文本。
_ANY_VERDICT_STR = st.one_of(
    st.sampled_from(sorted(_VALID_VERDICTS)),
    st.text(alphabet=st.characters(blacklist_categories=("Cs", "Cc"))),
)


class _StubParser:
    """可注入的 ``StructuredParser`` 替身。

    - ``outcome`` 给定时，``request_json`` 恒返回该固定 ``ParseOutcome``。
    - ``raises`` 给定时，``request_json`` 抛出该异常（模拟解析层失败冒泡）。
    """

    def __init__(
        self,
        *,
        outcome: ParseOutcome | None = None,
        raises: Exception | None = None,
    ) -> None:
        self._outcome = outcome
        self._raises = raises

    def request_json(self, messages, *, required_keys=()) -> ParseOutcome:
        if self._raises is not None:
            raise self._raises
        assert self._outcome is not None
        return self._outcome


# --------------------------------------------------------------------------- #
# Property 8: PARSED 采用合法枚举，非法值降级
# --------------------------------------------------------------------------- #

# Feature: citation-faithfulness-audit, Property 8: PARSED 采用合法枚举，非法值降级
@given(verdict_str=_ANY_VERDICT_STR)
@settings(max_examples=100)
def test_prop8_parsed_valid_enum_else_downgrade(verdict_str):
    """Validates: Requirements 3.3"""
    outcome = ParseOutcome(
        status=ParseStatus.PARSED,
        data={
            "verdict": verdict_str,
            "rationale": "r",
            "supporting_snippet": "s",
        },
    )
    judge = FaithfulnessJudge(_StubParser(outcome=outcome))

    verdict, _rationale, _snippet, parse_status = judge.judge(
        claim="c", grounding="g", reference_meta="m"
    )

    if verdict_str in _VALID_VERDICTS:
        # 合法枚举字符串：原样采用。
        assert verdict == FaithfulnessVerdict(verdict_str)
    else:
        # 非法值：经枚举 _missing_ 降级 cannot_verify。
        assert verdict == FaithfulnessVerdict.CANNOT_VERIFY

    # PARSED 路径下 parse_status 透传为 PARSED。
    assert parse_status == ParseStatus.PARSED
    # verdict 恒为枚举成员（全域闭合）。
    assert isinstance(verdict, FaithfulnessVerdict)


# --------------------------------------------------------------------------- #
# Property 9: 非 PARSED 或 grounding 不足绝不 supported（核心安全属性）
# --------------------------------------------------------------------------- #


def _non_supported_stubs():
    """构造一族「绝不应得出 supported/weak_support」的 stub parser。

    覆盖：FAILED、MOCK_FALLBACK、request_json 抛异常。
    """
    return st.one_of(
        st.just(
            _StubParser(
                outcome=ParseOutcome(status=ParseStatus.FAILED, data=None, reason="x")
            )
        ),
        st.just(
            _StubParser(
                outcome=ParseOutcome(
                    status=ParseStatus.MOCK_FALLBACK, data=None, reason="m"
                )
            )
        ),
        st.just(_StubParser(raises=ValueError("boom"))),
        st.just(_StubParser(raises=RuntimeError("kaboom"))),
    )


# Feature: citation-faithfulness-audit, Property 9: 非 PARSED 或 grounding 不足绝不 supported
@given(stub=_non_supported_stubs())
@settings(max_examples=100)
def test_prop9_non_parsed_never_supported(stub):
    """Validates: Requirements 3.4, 3.5, 3.6, 7.1"""
    judge = FaithfulnessJudge(stub)

    verdict, _rationale, _snippet, _parse_status = judge.judge(
        claim="c", grounding="g", reference_meta="m"
    )

    # 核心安全属性：非 PARSED / 异常路径绝不产出 supported 或 weak_support。
    assert verdict not in _SUPPORTED_LIKE
    assert verdict == FaithfulnessVerdict.CANNOT_VERIFY


# Feature: citation-faithfulness-audit, Property 9: 非 PARSED 或 grounding 不足绝不 supported
@given(verdict_str=_ANY_VERDICT_STR)
@settings(max_examples=100)
def test_prop9_parsed_only_valid_enum_may_be_supported(verdict_str):
    """Validates: Requirements 3.4, 3.5, 3.6, 7.1

    对偶断言：仅当 PARSED 且 verdict 为合法枚举时才可能出现 supported/weak_support。
    """
    outcome = ParseOutcome(
        status=ParseStatus.PARSED,
        data={"verdict": verdict_str, "rationale": "r", "supporting_snippet": "s"},
    )
    judge = FaithfulnessJudge(_StubParser(outcome=outcome))

    verdict, _rationale, _snippet, _parse_status = judge.judge(
        claim="c", grounding="g", reference_meta="m"
    )

    if verdict in _SUPPORTED_LIKE:
        # 若得出 supported/weak_support，则输入 verdict_str 必为对应的合法枚举值。
        assert verdict_str == verdict.value


# --------------------------------------------------------------------------- #
# Property 10: verdict 全域属于枚举且严重度映射为全函数
# --------------------------------------------------------------------------- #

_EXPECTED_SEVERITY = {
    FaithfulnessVerdict.UNSUPPORTED: "high",
    FaithfulnessVerdict.WEAK_SUPPORT: "medium",
    FaithfulnessVerdict.CANNOT_VERIFY: "low",
    FaithfulnessVerdict.SUPPORTED: "none",
}
_VALID_SEVERITIES = {"high", "medium", "low", "none"}


# Feature: citation-faithfulness-audit, Property 10: verdict 全域属于枚举且严重度映射为全函数
@given(verdict=st.sampled_from(list(FaithfulnessVerdict)))
@settings(max_examples=100)
def test_prop10_severity_is_total_function(verdict):
    """Validates: Requirements 4.1, 4.2, 4.3, 4.4, 4.5"""
    severity = severity_for(verdict)

    # 全函数：任一枚举成员均落在合法严重度域内。
    assert severity in _VALID_SEVERITIES
    # 精确映射。
    assert severity == _EXPECTED_SEVERITY[verdict]


# Feature: citation-faithfulness-audit, Property 10: verdict 全域属于枚举且严重度映射为全函数
@given(verdict_str=_ANY_VERDICT_STR)
@settings(max_examples=100)
def test_prop10_judge_verdict_always_enum_member(verdict_str):
    """Validates: Requirements 4.1, 4.2, 4.3, 4.4, 4.5

    judge 返回的 verdict 恒为枚举成员，可安全喂入全函数 severity_for。
    """
    outcome = ParseOutcome(
        status=ParseStatus.PARSED,
        data={"verdict": verdict_str, "rationale": "r", "supporting_snippet": "s"},
    )
    judge = FaithfulnessJudge(_StubParser(outcome=outcome))

    verdict, _rationale, _snippet, _parse_status = judge.judge(
        claim="c", grounding="g", reference_meta="m"
    )

    assert isinstance(verdict, FaithfulnessVerdict)
    # severity_for 对该返回值总有定义（全函数闭合）。
    assert severity_for(verdict) in _VALID_SEVERITIES
