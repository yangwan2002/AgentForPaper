"""Property-based tests for citation-faithfulness-audit 反馈闭环接入。

- Property 15: unsupported 驱动定位式 high 修订项（Req 6.1 / 6.3）。
- Property 16: 停用时逐字节不变（Req 6.4 / 6.5 / 8.1）。

生成器约束：所有生成文本一律排除 unicode 代理区与控制字符（categories
"Cs" / "Cc"），与既有忠实性属性测试保持一致。
"""

from __future__ import annotations

import copy
import tempfile

from hypothesis import given, settings
from hypothesis import strategies as st

from paper_agent.agents.base import AgentContext, AgentResult
from paper_agent.config import Config
from paper_agent.orchestrator import Orchestrator
from paper_agent.workspace.models import (
    InputMode,
    PaperWorkspace,
    ReviewRecord,
    SectionDraft,
)
from paper_agent.workspace.repository import WorkspaceRepository
from paper_agent.workspace.store import InMemoryStore

# --------------------------------------------------------------------------- #
# 生成器
# --------------------------------------------------------------------------- #

_SAFE_TEXT = st.text(
    alphabet=st.characters(blacklist_categories=("Cs", "Cc")), max_size=40
)
_ID_TEXT = st.text(
    alphabet="abcdefghijklmnopqrstuvwxyz0123456789", min_size=1, max_size=6
)
# 忠实性裁决中「非 unsupported」的取值——merge 通道对这些一律无操作。
_NON_UNSUPPORTED = st.sampled_from(["supported", "weak_support", "cannot_verify"])
_SEVERITIES = st.sampled_from(["high", "medium", "low", "none"])


def _make_ws() -> PaperWorkspace:
    return PaperWorkspace(
        workspace_id="w",
        input_mode=InputMode.GENERATION,
        topic_background="多智能体协作写作",
    )


@st.composite
def _ws_with_sections(draw) -> PaperWorkspace:
    """生成含 1..4 个章节草稿的最小工作区。"""
    ws = _make_ws()
    n = draw(st.integers(min_value=1, max_value=4))
    drafts: dict[str, SectionDraft] = {}
    for i in range(n):
        sid = f"s{i}"
        drafts[sid] = SectionDraft(
            section_id=sid, title=f"T{i}", content=draw(_SAFE_TEXT)
        )
    ws.section_drafts = drafts
    # 可选：附一条评审记录（含章节级反馈），使 _build_edits 产出非平凡 edits，
    # 从而 Property 16 能验证忠实性 merge 不会篡改这些既有 edits。
    if draw(st.booleans()):
        fb = {
            sid: draw(_SAFE_TEXT)
            for sid in drafts
            if draw(st.booleans())
        }
        ws.review_records = [ReviewRecord(iteration=1, section_feedback=fb)]
    return ws


@st.composite
def _finding(draw, sids, *, verdict_strategy) -> dict:
    """生成一条忠实性发现 dict（形状与被测代码读取的键一致）。"""
    return {
        "section_id": draw(st.sampled_from(sids)),
        "cited_reference_id": draw(_ID_TEXT),
        "verdict": draw(verdict_strategy),
        "severity": draw(_SEVERITIES),
        "rationale": draw(_SAFE_TEXT),
    }


@st.composite
def _ws_with_unsupported(draw) -> PaperWorkspace:
    """生成含 >=1 条 unsupported 发现（均绑定到存在的 section_id）的工作区。"""
    ws = draw(_ws_with_sections())
    sids = list(ws.section_drafts)
    any_verdict = st.sampled_from(
        ["unsupported", "supported", "weak_support", "cannot_verify"]
    )
    n = draw(st.integers(min_value=1, max_value=5))
    findings = [
        draw(_finding(sids, verdict_strategy=any_verdict)) for _ in range(n)
    ]
    # 强制至少一条 unsupported，绑定到一个存在的 section_id。
    forced = draw(st.integers(min_value=0, max_value=n - 1))
    findings[forced]["verdict"] = "unsupported"
    findings[forced]["section_id"] = draw(st.sampled_from(sids))
    ws.citation_faithfulness = findings
    return ws


@st.composite
def _ws_and_nonunsupported(draw):
    """生成 (工作区, 一组非 unsupported 发现) —— merge 通道应对其无操作。"""
    ws = draw(_ws_with_sections())
    sids = list(ws.section_drafts)
    findings = draw(
        st.lists(
            _finding(sids, verdict_strategy=_NON_UNSUPPORTED), max_size=6
        )
    )
    return ws, findings


# --------------------------------------------------------------------------- #
# 辅助：最小 Orchestrator 构造
# --------------------------------------------------------------------------- #


class _Dummy:
    """占位智能体：具备 name / run，_faithfulness_ok 与 _build_edits 均不会调用它。"""

    name = "dummy"

    def run(self, ctx: AgentContext) -> AgentResult:
        return AgentResult()


def _make_orchestrator(*, faithfulness_agent) -> Orchestrator:
    return Orchestrator(
        repo=WorkspaceRepository(InMemoryStore()),
        plan_agent=_Dummy(),
        search_agent=_Dummy(),
        writing_agent=_Dummy(),
        review_agent=_Dummy(),
        config=Config(workspace_dir=tempfile.mkdtemp(), iteration_limit=1),
        faithfulness_agent=faithfulness_agent,
    )


# --------------------------------------------------------------------------- #
# Property 15: unsupported 驱动定位式 high 修订项
# --------------------------------------------------------------------------- #

# Feature: citation-faithfulness-audit, Property 15: unsupported 驱动定位式 high 修订项
@given(ws=_ws_with_unsupported())
@settings(max_examples=100)
def test_prop15_unsupported_drives_located_gate_fix(ws):
    """Validates: Requirements 6.1, 6.3"""
    out = Orchestrator._build_edits(ws, [], None)

    unsupported_sids = {
        f["section_id"]
        for f in ws.citation_faithfulness
        if f.get("verdict") == "unsupported" and f["section_id"] in ws.section_drafts
    }
    assert unsupported_sids, "生成器应保证至少一条 unsupported 绑定到存在章节"

    # 每个 unsupported 的 section_id 都在 gate_fixes 中获得一条非空修订项。
    assert "gate_fixes" in out
    gate_fixes = out["gate_fixes"]
    for sid in unsupported_sids:
        assert sid in gate_fixes, f"section {sid} 应出现在 gate_fixes 中"
        assert gate_fixes[sid], f"section {sid} 的 gate_fixes 修订项不应为空"

    # 装配审计智能体时：存在 unsupported → _faithfulness_ok 为 False（Req 6.3）。
    orch = _make_orchestrator(faithfulness_agent=_Dummy())
    assert orch._faithfulness_ok(ws) is False

    # 当且仅当无 unsupported 时 _faithfulness_ok 为真：把 unsupported 全部改判后转真。
    ws_clean = copy.deepcopy(ws)
    for f in ws_clean.citation_faithfulness:
        if f.get("verdict") == "unsupported":
            f["verdict"] = "supported"
    assert orch._faithfulness_ok(ws_clean) is True


# --------------------------------------------------------------------------- #
# Property 16: 停用时逐字节不变
# --------------------------------------------------------------------------- #

# Feature: citation-faithfulness-audit, Property 16: 停用时逐字节不变
@given(data=_ws_and_nonunsupported())
@settings(max_examples=100)
def test_prop16_disabled_is_byte_for_byte_identical(data):
    """Validates: Requirements 6.4, 6.5, 8.1"""
    ws, findings = data
    orch = _make_orchestrator(faithfulness_agent=None)
    sid0 = next(iter(ws.section_drafts))

    # 未装配审计智能体 → _faithfulness_ok 恒为真，无视 citation_faithfulness 内容
    # （即便塞入 unsupported 发现，也不参与判定，Req 6.4 / 8.1）。
    ws_unsupported = copy.deepcopy(ws)
    ws_unsupported.citation_faithfulness = [
        {
            "section_id": sid0,
            "cited_reference_id": "x",
            "verdict": "unsupported",
            "severity": "high",
            "rationale": "r",
        }
    ]
    assert orch._faithfulness_ok(ws_unsupported) is True

    ws_empty_flag = copy.deepcopy(ws)
    ws_empty_flag.citation_faithfulness = []
    assert orch._faithfulness_ok(ws_empty_flag) is True

    # _build_edits：无 unsupported 发现时，忠实性 merge 通道为无操作 —— 空报告
    # 与「非 unsupported 报告」两条路径的输出逐字节一致（Req 6.5）。
    ws_empty = copy.deepcopy(ws)
    ws_empty.citation_faithfulness = []
    ws_pop = copy.deepcopy(ws)
    ws_pop.citation_faithfulness = findings

    out_empty = Orchestrator._build_edits(ws_empty, [], None)
    out_pop = Orchestrator._build_edits(ws_pop, [], None)
    assert out_empty == out_pop
