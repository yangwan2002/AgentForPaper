"""citation-faithfulness-audit 配置校验单元测试（task 2.2, Requirement 8.4）。

验证 ``Config.validate()`` 对忠实性审计阈值的「越界即回退到文档化默认」语义：

- 非法 ``min_grounding_chars``（负数）与非法 ``faithfulness_token_budget``
  （< 1，如 0 / 负数）在 ``validate()`` 运行时回退到文档化默认
  （``min_grounding_chars=40`` / ``faithfulness_token_budget=4000``），
  且**不**因这两个字段抛致命异常。
- 合法值经 ``validate()`` 原样保留、不被篡改。

与 format-pipeline 参数「越界即以 ValueError 拒绝」不同，这两个忠实性阈值
采取静默回退（就地重置属性）。本文件构造的 Config 仅让忠实性阈值非法、
其余字段全部合法，以隔离被测行为、避免其它字段触发 ValueError。
"""

from __future__ import annotations

import pytest

from paper_agent.config import Config

# 文档化默认值（见 Config 字段声明 / Req 8.3）。
_DEFAULT_MIN_GROUNDING_CHARS = 40
_DEFAULT_FAITHFULNESS_TOKEN_BUDGET = 4000


@pytest.mark.parametrize("illegal_min", [-1, -40, -1000])
def test_negative_min_grounding_chars_falls_back_to_default(illegal_min: int) -> None:
    """负 ``min_grounding_chars`` 经 validate() 回退到默认 40，不抛异常。"""
    config = Config(min_grounding_chars=illegal_min)

    # 不应因这个字段抛致命异常。
    config.validate()

    assert config.min_grounding_chars == _DEFAULT_MIN_GROUNDING_CHARS


@pytest.mark.parametrize("illegal_budget", [0, -1, -4000])
def test_illegal_faithfulness_token_budget_falls_back_to_default(
    illegal_budget: int,
) -> None:
    """< 1 的 ``faithfulness_token_budget`` 经 validate() 回退到默认 4000。"""
    config = Config(faithfulness_token_budget=illegal_budget)

    config.validate()

    assert config.faithfulness_token_budget == _DEFAULT_FAITHFULNESS_TOKEN_BUDGET


def test_both_illegal_thresholds_fall_back_without_raising() -> None:
    """两个阈值同时非法时都回退到默认，且 validate() 不抛异常。"""
    config = Config(min_grounding_chars=-5, faithfulness_token_budget=0)

    # 明确断言不抛致命异常（Req 8.4）。
    config.validate()

    assert config.min_grounding_chars == _DEFAULT_MIN_GROUNDING_CHARS
    assert config.faithfulness_token_budget == _DEFAULT_FAITHFULNESS_TOKEN_BUDGET


@pytest.mark.parametrize(
    ("legal_min", "legal_budget"),
    [
        (0, 1),  # 边界：min 最小合法值 0、budget 最小合法值 1
        (40, 4000),  # 文档化默认
        (100, 12000),  # 任意较大合法值
    ],
)
def test_legal_values_are_preserved_unchanged(
    legal_min: int, legal_budget: int
) -> None:
    """合法阈值经 validate() 原样保留、不被回退篡改。"""
    config = Config(
        min_grounding_chars=legal_min,
        faithfulness_token_budget=legal_budget,
    )

    config.validate()

    assert config.min_grounding_chars == legal_min
    assert config.faithfulness_token_budget == legal_budget
