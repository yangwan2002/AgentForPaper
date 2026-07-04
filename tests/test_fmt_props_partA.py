"""format-pipeline-and-diff-revision Part A 属性测试（Property 1–8）。

每条 Correctness Property 用单个 Hypothesis 属性测试实现（max_examples=100），
直接驱动 WritingAgent 的补丁物化 / 精确编辑 / 修订路由 / 整章重写契约校验，
以及 SectionEditTool 的 mode 校验；外部 LLM / 上下文管理器以确定性桩注入，
保证测试可重复且无网络依赖。
"""

from __future__ import annotations

from hypothesis import assume, given, settings
from hypothesis import strategies as st

from paper_agent.agents.revision_types import FallbackReason, RevisionRoute
from paper_agent.agents.writing_agent import WritingAgent
from paper_agent.context.manager import ContextManager
from paper_agent.providers.llm.base import LLMResponse
from paper_agent.providers.llm.mock import MockLLMProvider
from paper_agent.tools.section_edit_tool import SectionEditTool
from paper_agent.workspace.models import (
    InputMode,
    OutlineNode,
    PaperWorkspace,
    SectionDraft,
    SectionEdit,
)

_VALID_MODES = ("replace", "insert_after", "insert_before")
_ROUTE_VALUES = {RevisionRoute.PATCH_MODE.value, RevisionRoute.WHOLE_SECTION.value}
_FALLBACK_VALUES = {r.value for r in FallbackReason}

# 私有区哨兵字符：用于 Property 1 的插入标记，保证绝不出现在被测内容里。
_S_L = "\uE000"
_S_R = "\uE001"


# --------------------------------------------------------------------------- #
# 公共构造工具
# --------------------------------------------------------------------------- #


def _ws(sections: dict[str, str]) -> PaperWorkspace:
    """构造含给定章节草稿的最小工作区（section_id -> content）。"""
    ws = PaperWorkspace(
        workspace_id="w", input_mode=InputMode.GENERATION, topic_background="x"
    )
    for order, (sid, content) in enumerate(sections.items()):
        ws.outline.append(OutlineNode(section_id=sid, title=sid, order=order))
        ws.section_drafts[sid] = SectionDraft(
            section_id=sid, title=sid, content=content
        )
    return ws


def _agent(**kwargs) -> WritingAgent:
    """非工具模式的写作智能体，注入确定性 Mock LLM 与上下文管理器。"""
    return WritingAgent(
        MockLLMProvider(), ContextManager(MockLLMProvider()), **kwargs
    )


class _FakeSink:
    """捕获所有发出事件的假 sink，供载荷断言。"""

    def __init__(self) -> None:
        self.events: list = []

    def emit(self, event) -> None:
        self.events.append(event)


class _BadLLM:
    """整章重写桩 LLM：complete 恒返回含契约外构造（原始 HTML）的内容。"""

    def __init__(self, content: str) -> None:
        self._content = content
        self.calls: list = []

    def complete(self, messages, **opts):  # noqa: D401
        self.calls.append(messages)
        return LLMResponse(content=self._content)

    def stream(self, messages, **opts):  # pragma: no cover - 未走流式路径
        yield from ()


# --------------------------------------------------------------------------- #
# Property 1: 未触及文本的字节级保留
# --------------------------------------------------------------------------- #


@st.composite
def _content_and_insert_edits(draw):
    """生成内容 + 一组「纯插入」补丁（replacement 为唯一哨兵，anchor 多为内容切片）。"""
    content = draw(
        st.text(
            alphabet=st.characters(blacklist_characters=_S_L + _S_R),
            max_size=80,
        )
    )
    n = draw(st.integers(min_value=0, max_value=5))
    edits: list[SectionEdit] = []
    for i in range(n):
        mode = draw(st.sampled_from(["insert_after", "insert_before"]))
        if content and draw(st.booleans()):
            a = draw(st.integers(min_value=0, max_value=len(content) - 1))
            b = draw(st.integers(min_value=a + 1, max_value=len(content)))
            anchor = content[a:b]
        else:
            anchor = draw(st.text(max_size=6))
        sentinel = f"{_S_L}{i}{_S_R}"
        edits.append(
            SectionEdit(section_id="t", anchor=anchor, replacement=sentinel, mode=mode)
        )
    return content, edits


# Feature: format-pipeline-and-diff-revision, Property 1: 未触及文本的字节级保留
@settings(max_examples=100)
@given(_content_and_insert_edits())
def test_p1_untouched_text_byte_preserved(data):
    content, edits = data
    other = "非目标章节内容\n第二段"
    ws = _ws({"t": content, "other": other})
    agent = _agent(patch_size_limit=1e9)

    updated, _summaries, _logs = agent._materialize_edits(ws, edits)

    # 非目标章节：既不进入更新集合，工作区中亦逐字节不变。
    assert "other" not in updated
    assert ws.section_drafts["other"].content == other

    # 目标章节：凡未被成功补丁覆盖的字符（即所有原字符）逐字节保留——
    # 移除所有插入哨兵后必须精确还原原始内容。
    if "t" in updated:
        stripped = updated["t"].content
        for i in range(len(edits)):
            stripped = stripped.replace(f"{_S_L}{i}{_S_R}", "")
        assert stripped == content


# --------------------------------------------------------------------------- #
# Property 2: 锚点唯一性门控
# --------------------------------------------------------------------------- #


# Feature: format-pipeline-and-diff-revision, Property 2: 锚点唯一性门控
@settings(max_examples=100)
@given(
    fa=st.text(alphabet="0123456789 \n", max_size=20),
    mid=st.text(alphabet="0123456789 \n", max_size=20),
    fb=st.text(alphabet="0123456789 \n", max_size=20),
    n=st.sampled_from([0, 2, 3]),
)
def test_p2_anchor_uniqueness_gate(fa, mid, fb, n):
    unique_anchor = "UNIQ"
    dup_anchor = "DUP"
    # 构造：unique_anchor 恰 1 次；dup_anchor 恰 n 次（filler 仅含数字/空白，
    # 与字母锚点字符集不相交，命中次数可控）。
    dup_block = mid.join([dup_anchor] * n) if n else mid
    content = fa + unique_anchor + dup_block + fb + unique_anchor[:0]
    assert content.count(unique_anchor) == 1
    assert content.count(dup_anchor) == n

    ws = _ws({"t": content})
    agent = _agent(patch_size_limit=1e9)
    edits = [
        SectionEdit(section_id="t", anchor=unique_anchor, replacement="1", mode="replace"),
        SectionEdit(section_id="t", anchor=dup_anchor, replacement="ZZZ", mode="replace"),
    ]
    updated, _summaries, logs = agent._materialize_edits(ws, edits)

    # 唯一命中的补丁被应用；非唯一命中的补丁被跳过、其替换文本从不出现。
    assert "t" in updated
    expected = content.replace(unique_anchor, "1")
    assert updated["t"].content == expected
    assert "ZZZ" not in updated["t"].content
    # 跳过时日志记录含实际命中次数。
    assert any(f"实际命中 {n} 次" in line for line in logs)


# --------------------------------------------------------------------------- #
# Property 3: 补丁改动区间不重叠
# --------------------------------------------------------------------------- #


# Feature: format-pipeline-and-diff-revision, Property 3: 补丁改动区间不重叠
@settings(max_examples=100)
@given(
    fa=st.text(alphabet="0123456789 \n", max_size=20),
    fb=st.text(alphabet="0123456789 \n", max_size=20),
)
def test_p3_changed_intervals_no_overlap(fa, fb):
    anchor1 = "AAA"
    repl1 = "BBQBB"  # 含唯一标记 Q，供第二条补丁锚点落入本次改动区间
    content = fa + anchor1 + fb
    assert content.count(anchor1) == 1
    assert "Q" not in content

    ws = _ws({"t": content})
    agent = _agent(patch_size_limit=1e9)
    edits = [
        SectionEdit(section_id="t", anchor=anchor1, replacement=repl1, mode="replace"),
        # 第二条锚点 Q 落在第一条补丁的改动区间内 → 区间冲突应被跳过。
        SectionEdit(section_id="t", anchor="Q", replacement="Z", mode="replace"),
    ]
    updated, _summaries, logs = agent._materialize_edits(ws, edits)

    assert "t" in updated
    # 第二条补丁被跳过：内容等于仅应用第一条补丁的结果，Z 从不出现。
    assert updated["t"].content == content.replace(anchor1, repl1)
    assert "Z" not in updated["t"].content
    assert any("锚点区间冲突已跳过" in line for line in logs)


# --------------------------------------------------------------------------- #
# Property 4: 精确编辑的字节语义
# --------------------------------------------------------------------------- #


# Feature: format-pipeline-and-diff-revision, Property 4: 精确编辑的字节语义
@settings(max_examples=100)
@given(
    fa=st.text(alphabet="0123456789 \n", max_size=20),
    anchor=st.text(alphabet="abcdefghijklmnopqrstuvwxyz", min_size=1, max_size=10),
    fb=st.text(alphabet="0123456789 \n", max_size=20),
    repl=st.text(max_size=15),
)
def test_p4_exact_edit_byte_semantics(fa, anchor, fb, repl):
    content = fa + anchor + fb
    assume(content.count(anchor) == 1)  # 字母锚点 + 数字/空白 filler：几乎恒成立
    idx = len(fa)
    end = idx + len(anchor)

    # replace：仅置换锚点片段。
    out, ok = WritingAgent._apply_section_edit(
        content, SectionEdit(section_id="t", anchor=anchor, replacement=repl, mode="replace")
    )
    assert ok and out == content[:idx] + repl + content[end:]

    # insert_after：锚点后紧邻插入。
    out, ok = WritingAgent._apply_section_edit(
        content,
        SectionEdit(section_id="t", anchor=anchor, replacement=repl, mode="insert_after"),
    )
    assert ok and out == content[:end] + repl + content[end:]

    # insert_before：锚点前紧邻插入。
    out, ok = WritingAgent._apply_section_edit(
        content,
        SectionEdit(section_id="t", anchor=anchor, replacement=repl, mode="insert_before"),
    )
    assert ok and out == content[:idx] + repl + content[idx:]

    # 空 replacement：replace 删除锚点片段；insert_* 不改变字节序列。
    out, ok = WritingAgent._apply_section_edit(
        content, SectionEdit(section_id="t", anchor=anchor, replacement="", mode="replace")
    )
    assert ok and out == content[:idx] + content[end:]
    for mode in ("insert_after", "insert_before"):
        out, ok = WritingAgent._apply_section_edit(
            content,
            SectionEdit(section_id="t", anchor=anchor, replacement="", mode=mode),
        )
        assert ok and out == content


# --------------------------------------------------------------------------- #
# Property 5: 非法 mode 拒绝且内容不变
# --------------------------------------------------------------------------- #


# Feature: format-pipeline-and-diff-revision, Property 5: 非法 mode 拒绝且内容不变
@settings(max_examples=100)
@given(
    content=st.text(max_size=60),
    anchor=st.text(max_size=10),
    repl=st.text(max_size=10),
    mode=st.text(max_size=12).filter(lambda m: m not in _VALID_MODES),
)
def test_p5_invalid_mode_rejected(content, anchor, repl, mode):
    ws = _ws({"t": content})
    tool = SectionEditTool(ws)

    result = tool.edit_section("t", anchor, repl, mode=mode)

    # 拒绝应用、返回指示 mode 非法的错误、不累积任何编辑意图、内容字节级不变。
    assert isinstance(result, str) and "mode" in result and "非法" in result
    assert tool.edits == []
    assert ws.section_drafts["t"].content == content


# --------------------------------------------------------------------------- #
# Property 6: 修订路由完备且终止
# --------------------------------------------------------------------------- #


def _structural_targets(structural, sid: str) -> bool:
    if not structural:
        return False
    if sid in structural.get("remove", []):
        return True
    for node in structural.get("add", []):
        s = node.get("section_id") if isinstance(node, dict) else None
        if s == sid:
            return True
    return False


_structural_strategy = st.one_of(
    st.none(),
    st.builds(
        lambda rem, add: {"remove": rem, "add": add},
        rem=st.lists(st.sampled_from(["t", "x", "y"]), max_size=3),
        add=st.lists(
            st.builds(
                lambda s: {"section_id": s}, st.sampled_from(["t", "x", "y"])
            ),
            max_size=3,
        ),
    ),
)


# Feature: format-pipeline-and-diff-revision, Property 6: 修订路由完备且终止
@settings(max_examples=100)
@given(
    suggestion=st.text(max_size=40),
    structural=_structural_strategy,
    patch_first=st.booleans(),
)
def test_p6_revision_route_total_and_terminating(suggestion, structural, patch_first):
    ws = _ws({"t": "章节内容"})
    agent = _agent(patch_first_enabled=patch_first)

    route = agent._route_revision(ws, "t", suggestion, structural)

    # 完备且终止：恰返回枚举内单一取值。
    assert route in (RevisionRoute.PATCH_MODE, RevisionRoute.WHOLE_SECTION)
    assert route.value in _ROUTE_VALUES

    if _structural_targets(structural, "t"):
        assert route is RevisionRoute.WHOLE_SECTION
    elif patch_first:
        assert route is RevisionRoute.PATCH_MODE
    else:
        assert route is RevisionRoute.WHOLE_SECTION


# --------------------------------------------------------------------------- #
# Property 7: 整章重写不合规则保留原文
# --------------------------------------------------------------------------- #


# Feature: format-pipeline-and-diff-revision, Property 7: 整章重写不合规则保留原文
@settings(max_examples=100)
@given(
    original=st.text(max_size=80),
    tail=st.text(alphabet=st.characters(blacklist_characters="<>"), max_size=40),
    suggestion=st.text(max_size=30),
)
def test_p7_whole_section_noncompliant_preserves_original(original, tail, suggestion):
    # 桩 LLM 产出含契约外构造（原始 HTML 标签，offset 0 不在受保护区）的内容。
    bad_content = "<div>" + tail
    agent = WritingAgent(_BadLLM(bad_content), ContextManager(MockLLMProvider()))
    ws = _ws({"t": original})

    result = agent._localized_revision_llm(ws, {"t": suggestion}, None)
    for mutation in result.mutations:
        mutation(ws)

    # 产物含 unknown_construct → 丢弃、目标章节字节级不变、记录可诊断原因。
    assert ws.section_drafts["t"].content == original
    assert any(
        ("契约外构造" in line) or ("不合规" in line) for line in result.logs
    )


# --------------------------------------------------------------------------- #
# Property 8: 修订可观测载荷正确且脱敏
# --------------------------------------------------------------------------- #

_SECRET_KEYS = {"api_key", "authorization", "request_body", "prompt", "messages"}


@st.composite
def _obs_scenario(draw):
    fa = draw(st.text(alphabet="0123456789 \n", max_size=20))
    fb = draw(st.text(alphabet="0123456789 \n", max_size=20))
    token = "ANCHORTOKEN"
    content = fa + token + fb
    # 至少一条保证唯一命中的补丁，确保发出 patch_applied 事件。
    edits = [
        SectionEdit(section_id="t", anchor=token, replacement="REPLACED", mode="replace")
    ]
    extra = draw(
        st.lists(
            st.builds(
                lambda a, r, m: SectionEdit(
                    section_id="t", anchor=a, replacement=r, mode=m
                ),
                a=st.text(max_size=8),
                r=st.text(max_size=8),
                m=st.sampled_from(list(_VALID_MODES) + ["bogus"]),
            ),
            max_size=4,
        )
    )
    return content, edits + extra


# Feature: format-pipeline-and-diff-revision, Property 8: 修订可观测载荷正确且脱敏
@settings(max_examples=100)
@given(_obs_scenario())
def test_p8_revision_observability_payload(data):
    content, edits = data
    sink = _FakeSink()
    ws = _ws({"t": content})
    # 极大阈值避免触发「超过补丁适用上限」，保证走 patch_applied 事件路径。
    agent = _agent(patch_size_limit=1e9, sink=sink)

    _updated, _summaries, _logs = agent._materialize_edits(ws, edits)

    assert sink.events, "应至少发出一个修订事件"
    n_edits = len(edits)
    saw_patch_applied = False
    for ev in sink.events:
        d = ev.data
        # 脱敏：不含密钥/请求体键；任何字符串字段与 message ≤ 2000 字符。
        assert not (_SECRET_KEYS & set(d.keys()))
        assert len(ev.message) <= 2000
        for v in d.values():
            if isinstance(v, str):
                assert len(v) <= 2000
                assert "sk-" not in v
        # 路径取值合法。
        if "route" in d:
            assert d["route"] in _ROUTE_VALUES
        # 回退原因取值于固定枚举。
        if "fallback_reason" in d:
            assert d["fallback_reason"] in _FALLBACK_VALUES
        # patch_applied 载荷含 section_id 与正确的成功/跳过计数。
        if d.get("event") == "patch_applied":
            saw_patch_applied = True
            assert d["section_id"] == "t"
            assert d["route"] == RevisionRoute.PATCH_MODE.value
            assert d["applied"] >= 1
            assert d["skipped"] >= 0
            # 未提前中断（阈值极大）→ 每条补丁恰归入应用或跳过之一。
            assert d["applied"] + d["skipped"] == n_edits

    assert saw_patch_applied
