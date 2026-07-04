"""论文档案 / steering 测试。"""

from __future__ import annotations

from paper_agent.context.manager import ContextManager
from paper_agent.profile import PaperProfile, load_profile, render_profile
from paper_agent.providers.llm.mock import MockLLMProvider
from paper_agent.workspace.models import InputMode, OutlineNode, PaperWorkspace


def test_render_profile_empty_is_blank():
    assert render_profile({}) == ""
    assert render_profile(PaperProfile().to_dict()) == ""


def test_render_profile_includes_fields():
    p = PaperProfile(venue="RA-L", style="IEEE", instructions="强调创新点")
    text = render_profile(p.to_dict())
    assert "RA-L" in text and "IEEE" in text and "强调创新点" in text


def test_load_profile_parses_fields_and_instructions(tmp_path):
    f = tmp_path / "profile.md"
    f.write_text(
        "# 注释\n"
        "venue: NeurIPS\n"
        "style: ACM\n"
        "language: 中文\n"
        "强调方法创新；术语统一。\n",
        encoding="utf-8",
    )
    p = load_profile(str(f))
    assert p.venue == "NeurIPS"
    assert p.style == "ACM"
    assert p.language == "中文"
    assert "强调方法创新" in p.instructions


def test_profile_injected_into_stable_block():
    ws = PaperWorkspace(
        workspace_id="w", input_mode=InputMode.GENERATION, topic_background="x"
    )
    ws.outline = [OutlineNode(section_id="a", title="引言", order=0)]
    ws.profile = PaperProfile(venue="RA-L", style="IEEE").to_dict()
    cm = ContextManager(MockLLMProvider())
    block = cm.stable_block(ws)
    assert "RA-L" in block and "IEEE" in block
    assert "全局大纲" in block


def test_profile_persists_through_workspace_serialization():
    ws = PaperWorkspace(
        workspace_id="w", input_mode=InputMode.GENERATION, topic_background="x"
    )
    ws.profile = PaperProfile(venue="RA-L").to_dict()
    restored = PaperWorkspace.from_dict(ws.to_dict())
    assert restored.profile["venue"] == "RA-L"
