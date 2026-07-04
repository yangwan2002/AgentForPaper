"""单一写路径 apply/commit 单元测试（任务 3）。

验证：
- 仅 accepted_mutations 落盘，rejected 不落盘（单一写路径 + Property 1）；
- 空接受意图为 no-op；
- 批量应用期异常 → 全回滚，无部分写入（Property 3）；
- 批量落盘期异常 → 全回滚（Property 3）；
- commit 先过闸门再落盘并返回 GateOutcome。
"""

from __future__ import annotations

import copy

import pytest

from paper_agent.agent_platform.apply import apply_screened, commit
from paper_agent.agent_platform.guardrail_gate import GuardrailGate
from paper_agent.agent_platform.models import (
    CHANGE_CONTENT,
    GateOutcome,
    ProposedChange,
    RejectedChange,
)
from paper_agent.workspace.models import (
    InputMode,
    OutlineNode,
    PaperWorkspace,
    SectionDraft,
)
from paper_agent.workspace.repository import WorkspaceRepository


class _MemStore:
    """内存 store；可配置在第 N 次 save 时抛错，用于测落盘失败回滚。"""

    def __init__(self, fail_on_save_call=None):
        self._data = {}
        self._save_calls = 0
        self._fail_on = fail_on_save_call

    def load(self, workspace_id):
        raw = self._data.get(workspace_id)
        return PaperWorkspace.from_dict(raw) if raw else None

    def save(self, ws):
        self._save_calls += 1
        if self._fail_on is not None and self._save_calls == self._fail_on:
            raise RuntimeError("模拟落盘失败")
        # 深拷贝：模拟真实 JsonFileStore 的 JSON 序列化语义（不共享活对象引用）。
        self._data[ws.workspace_id] = copy.deepcopy(ws.to_dict())


def _repo(fail_on_save_call=None):
    return WorkspaceRepository(_MemStore(fail_on_save_call=fail_on_save_call))


def _ws():
    ws = PaperWorkspace(workspace_id="w1", input_mode=InputMode.DRAFT_REVISION)
    ws.outline = [OutlineNode(section_id="s", title="S", order=0)]
    ws.section_drafts = {"s": SectionDraft(section_id="s", title="S", content="orig")}
    return ws


def _set(section_id, text):
    def _mut(ws):
        ws.section_drafts[section_id].content = text
    return _mut


# --- apply_screened ----------------------------------------------------------

def test_apply_empty_is_noop():
    repo = _repo()
    ws = repo.create(_ws())
    outcome = GateOutcome(passed=True, accepted_mutations=[])
    apply_screened(repo, ws, outcome)
    assert ws.section_drafts["s"].content == "orig"


def test_apply_persists_accepted_only():
    repo = _repo()
    ws = repo.create(_ws())
    outcome = GateOutcome(
        passed=True,
        accepted_mutations=[_set("s", "changed")],
        rejected=[RejectedChange(section_id="s", reason="x")],
    )
    apply_screened(repo, ws, outcome)
    # 内存已更新。
    assert ws.section_drafts["s"].content == "changed"
    # 重新从 store 读，确认已落盘。
    reloaded = repo.load("w1")
    assert reloaded.section_drafts["s"].content == "changed"


def test_apply_rolls_back_on_application_error():
    repo = _repo()
    ws = repo.create(_ws())

    def _boom(ws):
        raise ValueError("应用期炸了")

    outcome = GateOutcome(
        passed=True,
        accepted_mutations=[_set("s", "first"), _boom],
    )
    with pytest.raises(ValueError):
        apply_screened(repo, ws, outcome)
    # 全回滚：即使第一个意图已应用，整批失败后工作区回到批前状态。
    assert ws.section_drafts["s"].content == "orig"
    assert repo.load("w1").section_drafts["s"].content == "orig"


def test_apply_rolls_back_on_save_error():
    # 第一次 save 是 create；第二次 save（update）失败。
    repo = _repo(fail_on_save_call=2)
    ws = repo.create(_ws())
    outcome = GateOutcome(passed=True, accepted_mutations=[_set("s", "changed")])
    with pytest.raises(RuntimeError):
        apply_screened(repo, ws, outcome)
    assert ws.section_drafts["s"].content == "orig"
    assert repo.load("w1").section_drafts["s"].content == "orig"


# --- commit ------------------------------------------------------------------

def test_commit_screens_then_persists():
    repo = _repo()
    ws = repo.create(_ws())
    gate = GuardrailGate()  # 无护栏 → 内容改动直接通过
    change = ProposedChange(
        mutation=_set("s", "committed"), kind=CHANGE_CONTENT, section_id="s"
    )
    outcome = commit(repo, ws, gate, [change])
    assert outcome.passed is True
    assert repo.load("w1").section_drafts["s"].content == "committed"


def test_commit_rejected_change_not_persisted():
    class _Q:
        def check(self, ws):
            class R:
                issues = [{"type": "placeholder", "severity": "high", "section_id": "s", "message": "坏"}]
            return R()

    repo = _repo()
    ws = repo.create(_ws())
    gate = GuardrailGate(quality_gate=_Q())
    change = ProposedChange(
        mutation=_set("s", "bad"), kind=CHANGE_CONTENT, section_id="s"
    )
    outcome = commit(repo, ws, gate, [change])
    assert outcome.passed is False
    # 被拒改动未落盘。
    assert repo.load("w1").section_drafts["s"].content == "orig"
