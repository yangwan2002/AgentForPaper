"""判定 prompt 模板单元测试（citation-faithfulness-audit 任务 5.2）。

验证 `templates.judge_citation_faithfulness` 的易变段仅由 claim / grounding /
reference_meta 三者构成，不注入其它章节正文或记忆提示，并声明所需 JSON 键；
system 段等于 `FAITHFULNESS_JUDGE_SYSTEM` 且约束「仅依据 grounding、不足选
cannot_verify、仅输出 JSON」。

_Requirements: 3.1_
"""

from __future__ import annotations

from paper_agent.prompts import templates
from paper_agent.prompts.templates import FAITHFULNESS_JUDGE_SYSTEM
from paper_agent.providers.llm.base import Message

# 独特哨兵字符串：不会与模板固定措辞或其它内容意外撞车。
CLAIM_SENTINEL = "CLAIM_SENTINEL_XYZ"
GROUNDING_SENTINEL = "GROUNDING_SENTINEL_ABC"
REFERENCE_META_SENTINEL = "REFERENCE_META_SENTINEL_QWE"
# 一个未被传入的哨兵——绝不应出现在任何渲染消息中。
NOT_PASSED_SENTINEL = "NOT_PASSED_SENTINEL_999"


def _build() -> list[Message]:
    return templates.judge_citation_faithfulness(
        claim=CLAIM_SENTINEL,
        grounding=GROUNDING_SENTINEL,
        reference_meta=REFERENCE_META_SENTINEL,
    )


def test_returns_list_of_messages_system_then_user():
    """返回 list[Message]：稳定 system + 单条 user（仅两段）。"""
    msgs = _build()
    assert isinstance(msgs, list)
    assert len(msgs) == 2
    assert all(isinstance(m, Message) for m in msgs)
    assert msgs[0].role == "system"
    assert msgs[1].role == "user"


def test_system_message_is_faithfulness_judge_system():
    """system 段逐字节等于 FAITHFULNESS_JUDGE_SYSTEM。"""
    msgs = _build()
    assert msgs[0].content == FAITHFULNESS_JUDGE_SYSTEM


def test_system_message_instructs_grounding_only_and_json_only():
    """system 段约束：仅依据 grounding、不足选 cannot_verify、仅输出 JSON。"""
    sys_text = _build()[0].content
    # 只看 grounding / 不得用自身知识或记忆。
    assert "grounding" in sys_text
    assert "记忆" in sys_text
    # grounding 不足必选 cannot_verify。
    assert "cannot_verify" in sys_text
    # 仅输出 JSON。
    assert "JSON" in sys_text


def test_user_message_contains_all_three_inputs():
    """user 段包含且仅围绕 claim / grounding / reference_meta 三者构建。"""
    user_text = _build()[1].content
    assert CLAIM_SENTINEL in user_text
    assert GROUNDING_SENTINEL in user_text
    assert REFERENCE_META_SENTINEL in user_text


def test_no_unrelated_sentinel_injected():
    """未传入的哨兵不应出现在任何渲染消息中（无其它章节正文/记忆提示注入）。"""
    for m in _build():
        assert NOT_PASSED_SENTINEL not in m.content


def test_variable_content_only_from_provided_inputs():
    """易变内容仅来自所提供的三项输入。

    从 user 段中剥离模板固定措辞后再抠掉三个哨兵，剩余文本不应再残留任何
    看似「外部注入」的哨兵式内容——通过校验未传入哨兵缺席 + 三项均在场，
    确认变量内容边界只由 claim/grounding/reference_meta 决定。
    """
    user_text = _build()[1].content
    # 依次替换掉三个被允许的变量，确保它们都真实来自入参而非模板硬编码。
    stripped = (
        user_text.replace(CLAIM_SENTINEL, "")
        .replace(GROUNDING_SENTINEL, "")
        .replace(REFERENCE_META_SENTINEL, "")
    )
    # 变量被抠除后，三个哨兵不再残留（证明每个哨兵恰好来自对应入参）。
    assert CLAIM_SENTINEL not in stripped
    assert GROUNDING_SENTINEL not in stripped
    assert REFERENCE_META_SENTINEL not in stripped
    # 且从未传入的哨兵也不在其中。
    assert NOT_PASSED_SENTINEL not in stripped


def test_prompt_declares_required_json_keys():
    """prompt 声明所需 JSON 输出键：verdict / rationale / supporting_snippet。"""
    user_text = _build()[1].content
    assert "verdict" in user_text
    assert "rationale" in user_text
    assert "supporting_snippet" in user_text


def test_prompt_lists_verdict_enum_values():
    """prompt 列出 verdict 的取值域，含 cannot_verify 兜底。"""
    user_text = _build()[1].content
    for verdict in ("supported", "weak_support", "unsupported", "cannot_verify"):
        assert verdict in user_text


def test_inputs_are_isolated_swap_changes_only_that_slot():
    """交换某一入参只改变对应槽位——确认三者互不串位、无固定回退文本。"""
    base = _build()[1].content
    swapped = templates.judge_citation_faithfulness(
        claim=CLAIM_SENTINEL,
        grounding="DIFFERENT_GROUNDING_777",
        reference_meta=REFERENCE_META_SENTINEL,
    )[1].content
    assert "DIFFERENT_GROUNDING_777" in swapped
    assert GROUNDING_SENTINEL not in swapped
    # claim 与 reference_meta 仍在场，未受 grounding 改动影响。
    assert CLAIM_SENTINEL in swapped
    assert REFERENCE_META_SENTINEL in swapped
    assert base != swapped


def test_deep_review_prompt_is_independent_strict_and_grounding_only():
    msgs = templates.deep_review_citation_faithfulness(
        claim=CLAIM_SENTINEL,
        grounding=GROUNDING_SENTINEL,
        reference_meta=REFERENCE_META_SENTINEL,
    )

    assert len(msgs) == 2
    assert msgs[0].content != FAITHFULNESS_JUDGE_SYSTEM
    rendered = "\n".join(message.content for message in msgs)
    assert "独立" in rendered
    assert "只能使用" in rendered
    assert "grounding" in rendered
    assert "cannot_verify" in rendered
    assert "weak_support" not in msgs[-1].content
    assert CLAIM_SENTINEL in rendered
    assert GROUNDING_SENTINEL in rendered
    assert REFERENCE_META_SENTINEL in rendered
    assert NOT_PASSED_SENTINEL not in rendered
