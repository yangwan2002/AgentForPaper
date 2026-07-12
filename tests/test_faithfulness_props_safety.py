"""Property-based tests for citation-faithfulness-audit 安全不变量。

- Property 4: 未验证引用标记且不触发判定器（Req 1.5）。
- Property 18: 不可信文本纯字符串处理（Req 7.3）。

生成器约束：参与抽取/判定的字符串一律排除 unicode 代理区与控制字符
（categories "Cs" / "Cc"），避免代理/控制字符导致的无关失败。
"""

from __future__ import annotations

import builtins

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from paper_agent.agents.base import AgentContext
from paper_agent.agents.citation_faithfulness_agent import (
    CitationFaithfulnessAgent,
    FaithfulnessJudge,
)
from paper_agent.parsing.structured_parser import ParseOutcome
from paper_agent.tools.faithfulness_extract import extract_pairs
from paper_agent.workspace.faithfulness import FaithfulnessVerdict
from paper_agent.workspace.models import (
    InputMode,
    OutputFormat,
    PaperWorkspace,
    ParseStatus,
    ReferenceEntry,
    SectionDraft,
)

# --------------------------------------------------------------------------- #
# 生成器（排除 unicode 代理区 "Cs" 与控制字符 "Cc"，长度受限）
# --------------------------------------------------------------------------- #

_SAFE_TEXT = st.text(
    alphabet=st.characters(blacklist_categories=("Cs", "Cc")), max_size=40
)
# 引用 id 仅取 quality_gate._TEXT_CITATION 认可的 ASCII 标识符字符子集。
_ID_TEXT = st.text(
    alphabet="abcdefghijklmnopqrstuvwxyz0123456789", min_size=1, max_size=6
)

# 允许出现在报告中的严重度取值（severity_for 的值域）。
_ALLOWED_SEVERITY = {"high", "medium", "low", "none"}
_ALLOWED_VERDICTS = {v.value for v in FaithfulnessVerdict}
_REPORT_KEYS = {
    "section_id",
    "cited_reference_id",
    "claim_excerpt",
    "verdict",
    "severity",
    "rationale",
    "supporting_snippet",
    "parse_status",
    "unverified_reference",
}

# 注入式/模板式载荷：这些串只应作为纯数据被处理，绝不被求值或执行。
_INJECTION_PAYLOADS = [
    '__import__("os").system("echo pwned")',
    "eval(\"1 + 1\")",
    "exec('x = 1')",
    "{{template_var}}",
    "${x}",
    "`backtick_cmd`",
    "%s%d%n",
    "{0}{1}",
    "os.system('rm -rf /')",
]


def _base_ws() -> PaperWorkspace:
    return PaperWorkspace(
        workspace_id="ws-safety",
        input_mode=InputMode.GENERATION,
        output_format=OutputFormat.MARKDOWN,
        topic_background="安全不变量测试",
    )


class _StubParser:
    """桩 StructuredParser：始终 PARSED（supported），供判定器读取。"""

    def request_json(self, messages, *, required_keys=()) -> ParseOutcome:  # noqa: D401
        return ParseOutcome(
            status=ParseStatus.PARSED,
            data={"verdict": "supported", "rationale": "", "supporting_snippet": ""},
        )


class _SpyJudge:
    """替换 ``FaithfulnessJudge`` 的间谍：记录调用次数并返回确定性 PARSED 裁决。

    ``CitationFaithfulnessAgent`` 仅以关键字参数调用 ``self._judge.judge(...)``，
    故任何具备同签名 ``judge`` 方法的对象均可注入（鸭子类型）。
    """

    def __init__(self) -> None:
        self.calls = 0

    def judge(
        self, *, claim: str, grounding: str, reference_meta: str
    ) -> tuple[FaithfulnessVerdict, str, str, ParseStatus]:
        self.calls += 1
        return (FaithfulnessVerdict.SUPPORTED, "", "", ParseStatus.PARSED)


@st.composite
def _mixed_workspace(draw) -> PaperWorkspace:
    """生成含「已验证 + 未验证」引用混合的工作区。

    - ``verified_ids``：写入 ``verified_references`` 且 ``verified=True``。
    - ``unverified_ids``：仅在正文被引用、不入库（触发 unverified 对）。
    - 每个章节正文由随机安全文本 + 若干 ``[id]`` 声明句拼接而成。
    """
    verified_ids = draw(st.lists(_ID_TEXT, min_size=0, max_size=4, unique=True))
    extra = draw(st.lists(_ID_TEXT, min_size=0, max_size=3, unique=True))
    unverified_ids = [e for e in extra if e not in verified_ids]
    citeable = list(verified_ids) + unverified_ids

    section_drafts: dict[str, SectionDraft] = {}
    n_sections = draw(st.integers(min_value=0, max_value=3))
    for i in range(n_sections):
        parts = [draw(_SAFE_TEXT)]
        cited = (
            draw(st.lists(st.sampled_from(citeable), max_size=4)) if citeable else []
        )
        for cid in cited:
            parts.append(f" 声明句 [{cid}]。")
        sid = f"s{i}"
        section_drafts[sid] = SectionDraft(
            section_id=sid, title=f"T{i}", content="".join(parts)
        )

    refs = [
        ReferenceEntry(
            id=rid,
            title=f"Title {rid}",
            authors=["A. Author"],
            year=2020,
            source_id=rid,
            source="arxiv",
            verified=True,
            abstract=f"Abstract text for reference {rid}, sufficiently long.",
        )
        for rid in verified_ids
    ]

    ws = _base_ws()
    ws.verified_references = refs
    ws.section_drafts = section_drafts
    return ws


def _pair_counts(ws: PaperWorkspace) -> tuple[int, int]:
    """用与被测代码相同的 extract_pairs 计算 (已验证对总数, 未验证对总数)。"""
    verified = ws.verified_reference_ids()
    n_verified = n_unverified = 0
    for sid, draft in ws.section_drafts.items():
        vp, up = extract_pairs(sid, draft.content or "", verified)
        n_verified += len(vp)
        n_unverified += len(up)
    return n_verified, n_unverified


# --------------------------------------------------------------------------- #
# Property 4: 未验证引用标记且不触发判定器
# --------------------------------------------------------------------------- #

# Feature: citation-faithfulness-audit, Property 4: 未验证引用标记且不触发判定器
@given(ws=_mixed_workspace())
@settings(max_examples=150)
def test_prop4_unverified_marked_and_judge_not_invoked(ws):
    """Validates: Requirements 1.5"""
    verified_ids = ws.verified_reference_ids()
    n_verified_pairs, n_unverified_pairs = _pair_counts(ws)

    spy = _SpyJudge()
    # min_grounding_chars=0：已验证对的 grounding 非空即充足，保证每个已验证对触发判定。
    agent = CitationFaithfulnessAgent(spy, min_grounding_chars=0, token_budget=200)

    result = agent.run(AgentContext(workspace=ws))

    # 单独在干净工作区上应用 mutation 读取报告。
    scratch = _base_ws()
    for mut in result.mutations:
        mut(scratch)
    report = list(scratch.citation_faithfulness)

    # (a) 判定器仅为已验证对被调用，从不为未验证对调用。
    #     重复的「同声明、同文献」可在同轮命中缓存；未验证对不增加调用。
    assert spy.calls <= n_verified_pairs
    if n_verified_pairs == 0:
        assert spy.calls == 0

    # (b) 每个 unverified_reference=True 的发现：verdict 为 cannot_verify，
    #     且其 cited_reference_id 不属于已验证集合。
    unverified_findings = [f for f in report if f["unverified_reference"]]
    assert len(unverified_findings) == n_unverified_pairs
    for f in unverified_findings:
        assert f["verdict"] == FaithfulnessVerdict.CANNOT_VERIFY.value
        assert f["cited_reference_id"] not in verified_ids

    # (c) 反向：任何 cited_reference_id 属于已验证集合的发现都不应被标记为未验证。
    for f in report:
        if f["cited_reference_id"] in verified_ids:
            assert f["unverified_reference"] is False


# --------------------------------------------------------------------------- #
# Property 18: 不可信文本纯字符串处理
# --------------------------------------------------------------------------- #

@st.composite
def _payload_text(draw) -> str:
    """生成掺入注入式载荷的不可信文本（安全文本 + 若干载荷片段交织）。"""
    chunks: list[str] = [draw(_SAFE_TEXT)]
    payloads = draw(
        st.lists(st.sampled_from(_INJECTION_PAYLOADS), min_size=1, max_size=4)
    )
    for p in payloads:
        chunks.append(p)
        chunks.append(draw(_SAFE_TEXT))
    return " ".join(chunks)


# Feature: citation-faithfulness-audit, Property 18: 不可信文本纯字符串处理
@given(claim_text=_payload_text(), title_text=_payload_text(), abstract_text=_payload_text())
@settings(max_examples=100)
def test_prop18_untrusted_text_string_only(claim_text, title_text, abstract_text):
    """Validates: Requirements 7.3"""
    rid = "r0"
    ws = _base_ws()
    ws.verified_references = [
        ReferenceEntry(
            id=rid,
            title=title_text,
            authors=["A. Author"],
            year=2021,
            source_id=rid,
            source="arxiv",
            verified=True,
            abstract=abstract_text,
        )
    ]
    # 正文声明句内嵌载荷，并以 [r0] 挂靠已验证文献 → 触发完整 grounding + 判定路径。
    ws.section_drafts = {
        "s0": SectionDraft(
            section_id="s0",
            title="Intro",
            content=f"{claim_text} [{rid}]。",
        )
    }

    agent = CitationFaithfulnessAgent(
        FaithfulnessJudge(_StubParser()), min_grounding_chars=0, token_budget=200
    )

    # 监视 builtins.eval / exec：若审计过程中任何一处求值/执行了不可信文本，即视为失败。
    orig_eval, orig_exec = builtins.eval, builtins.exec

    def _forbidden_eval(*a, **k):
        raise AssertionError("eval() was invoked on untrusted text")

    def _forbidden_exec(*a, **k):
        raise AssertionError("exec() was invoked on untrusted text")

    builtins.eval = _forbidden_eval
    builtins.exec = _forbidden_exec
    try:
        result = agent.run(AgentContext(workspace=ws))
    finally:
        builtins.eval = orig_eval
        builtins.exec = orig_exec

    # run 正常完成，产出可原子应用的 mutation。
    scratch = _base_ws()
    for mut in result.mutations:
        mut(scratch)
    report = list(scratch.citation_faithfulness)

    # 报告结构良好：字段齐全、verdict/severity 取值合法，且报告可 JSON 序列化。
    import json

    json.dumps(report)  # 不抛异常即证明纯数据（无不可序列化对象/副作用）。

    for f in report:
        assert set(f.keys()) == _REPORT_KEYS
        assert f["verdict"] in _ALLOWED_VERDICTS
        assert f["severity"] in _ALLOWED_SEVERITY
        # 载荷作为纯字符串数据出现在摘要中（截断后为原文前缀），从不被求值。
        assert isinstance(f["claim_excerpt"], str)


if __name__ == "__main__":  # pragma: no cover
    pytest.main([__file__, "-q"])
