"""visual-layout-acceptance 波2 测试：变化页选择 + 视觉判断解析。

覆盖 Property 12（变化页选择）与视觉判断防御式解析。不打真实 vision API。
"""

from __future__ import annotations

import json

from paper_agent.agent_platform.visual.judge import VisualJudge, VisualVerdict
from paper_agent.agent_platform.visual.page_select import select_pages_to_judge
from paper_agent.providers.llm.base import LLMResponse


def _pages(tmp_path, n, tag=""):
    """造 n 张内容可控的假 png，返回路径列表。"""
    out = []
    for i in range(n):
        p = tmp_path / f"{tag}p{i}.png"
        p.write_bytes(f"{tag}-page-{i}".encode())
        out.append(str(p))
    return out


# --------------------------------------------------------------------------- #
# Property 12: 变化页选择
# --------------------------------------------------------------------------- #
def test_no_baseline_returns_first_max_pages(tmp_path):
    after = _pages(tmp_path, 5, "a")
    pages, sampled = select_pages_to_judge(None, after, max_pages=3)
    assert pages == after[:3]
    assert sampled is True            # 5 > 3 → 截断


def test_no_baseline_under_cap_not_sampled(tmp_path):
    after = _pages(tmp_path, 2, "a")
    pages, sampled = select_pages_to_judge(None, after, max_pages=6)
    assert pages == after
    assert sampled is False


def test_only_changed_pages_selected(tmp_path):
    before = _pages(tmp_path, 4, "same")   # before/after 前 3 页相同、第 4 页不同
    after = list(before)
    # 让第 2 页（index 2）内容变化。
    changed = tmp_path / "changed_p2.png"
    changed.write_bytes(b"DIFFERENT CONTENT")
    after[2] = str(changed)
    pages, sampled = select_pages_to_judge(before, after, max_pages=10, neighbor=1)
    # 变化页 index 2 + 邻页 1、3 → 选中 index 1,2,3。
    assert after[1] in pages and after[2] in pages and after[3] in pages
    assert after[0] not in pages
    assert sampled is False


def test_extra_after_pages_are_changed(tmp_path):
    before = _pages(tmp_path, 2, "same")
    after = list(before) + _pages(tmp_path, 2, "extra")  # 多出 2 页（回流增页）
    pages, _sampled = select_pages_to_judge(before, after, max_pages=10, neighbor=0)
    assert after[2] in pages and after[3] in pages


def test_identical_before_after_returns_min_sample(tmp_path):
    imgs = _pages(tmp_path, 3, "x")
    pages, _sampled = select_pages_to_judge(list(imgs), list(imgs), max_pages=2)
    assert len(pages) == 2            # 无变化 → 给最小样本，不空手


def test_selection_never_exceeds_max_pages(tmp_path):
    before = _pages(tmp_path, 8, "b")
    after = _pages(tmp_path, 8, "a")   # 全变
    pages, sampled = select_pages_to_judge(before, after, max_pages=3, neighbor=1)
    assert len(pages) <= 3
    assert sampled is True


# --------------------------------------------------------------------------- #
# 视觉判断防御式解析
# --------------------------------------------------------------------------- #
class _VLM:
    def __init__(self, content):
        self._content = content
        self.calls = 0

    def complete(self, messages, **opts):
        self.calls += 1
        return LLMResponse(content=self._content)


def test_judge_parses_valid_verdict(tmp_path):
    imgs = _pages(tmp_path, 1, "j")
    vlm = _VLM(json.dumps({"satisfied": False, "defects": ["图1上方大片空白"], "advisory": "让图上移"}))
    v = VisualJudge(vlm).judge(imgs, "图放页顶跨双栏")
    assert v.parsed is True
    assert v.satisfied is False
    assert "图1上方大片空白" in v.defects


def test_judge_satisfied(tmp_path):
    imgs = _pages(tmp_path, 1, "j")
    vlm = _VLM('{"satisfied": true, "defects": [], "advisory": ""}')
    v = VisualJudge(vlm).judge(imgs, "正文两栏")
    assert v.parsed is True and v.satisfied is True


def test_judge_bad_json_is_unparsed(tmp_path):
    imgs = _pages(tmp_path, 1, "j")
    v = VisualJudge(_VLM("这不是JSON，我觉得还行")).judge(imgs, "随便")
    assert v.parsed is False
    assert v.satisfied is False       # 不可信 → 不当作通过


def test_judge_empty_pages_unparsed():
    v = VisualJudge(_VLM("{}")).judge([], "诉求")
    assert v.parsed is False


def test_judge_vlm_exception_is_unparsed(tmp_path):
    imgs = _pages(tmp_path, 1, "j")

    class _Boom:
        def complete(self, messages, **opts):
            raise RuntimeError("network down")

    v = VisualJudge(_Boom()).judge(imgs, "诉求")
    assert v.parsed is False
