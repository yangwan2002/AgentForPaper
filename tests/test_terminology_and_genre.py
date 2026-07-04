"""Round 8 质量档测试：术语抽取 + 体裁化润色/评审。"""

from __future__ import annotations

from paper_agent.agents.base import AgentContext
from paper_agent.agents.terminology_agent import TerminologyAgent
from paper_agent.parsing.structured_parser import ParseOutcome
from paper_agent.prompts import templates
from paper_agent.workspace.models import (
    InputMode,
    OutlineNode,
    ParseStatus,
    PaperWorkspace,
    SectionDraft,
)


class _StubParser:
    def __init__(self, status, data=None):
        self._outcome = ParseOutcome(status=status, data=data)

    def request_json(self, messages, *, required_keys=(), **kw):
        self.last_messages = messages
        return self._outcome


def _ws():
    ws = PaperWorkspace(workspace_id="w", input_mode=InputMode.GENERATION)
    ws.outline.append(OutlineNode(section_id="method", title="方法", order=0))
    ws.section_drafts["method"] = SectionDraft(
        section_id="method", title="方法", content="我们提出跨视角匹配网络，用于图像匹配。"
    )
    return ws


# --- 术语抽取 ---


def test_terminology_populates_glossary():
    data = {"terms": [{"term": "跨视角匹配网络", "definition": "本文方法"},
                      {"term": "SLAM", "definition": ""}]}
    agent = TerminologyAgent(
        llm=None, parser=_StubParser(ParseStatus.PARSED, data), is_mock=False
    )
    ws = _ws()
    result = agent.run(AgentContext(workspace=ws))
    for m in result.mutations:
        m(ws)
    assert ws.glossary["跨视角匹配网络"] == "本文方法"
    assert "SLAM" in ws.glossary


def test_terminology_does_not_override_user_terms():
    data = {"terms": [{"term": "SLAM", "definition": "系统抽取的定义"}]}
    agent = TerminologyAgent(
        llm=None, parser=_StubParser(ParseStatus.PARSED, data), is_mock=False
    )
    ws = _ws()
    ws.glossary["SLAM"] = "用户定义"
    result = agent.run(AgentContext(workspace=ws))
    for m in result.mutations:
        m(ws)
    assert ws.glossary["SLAM"] == "用户定义"  # setdefault 不覆盖


def test_terminology_mock_is_noop():
    agent = TerminologyAgent(llm=None, is_mock=True)
    ws = _ws()
    result = agent.run(AgentContext(workspace=ws))
    assert result.mutations == []


def test_terminology_non_parsed_noop():
    agent = TerminologyAgent(
        llm=None, parser=_StubParser(ParseStatus.FAILED), is_mock=False
    )
    ws = _ws()
    result = agent.run(AgentContext(workspace=ws))
    assert result.mutations == []


# --- 体裁化 prompt 注入 ---


def test_polish_section_injects_guidance():
    msgs = templates.polish_section(
        title="摘要", content="正文", glossary_terms="", section_guidance="摘要应 150-250 词"
    )
    user_text = "\n".join(m.content for m in msgs)
    assert "150-250 词" in user_text
    assert "体裁语言惯例" in user_text


def test_review_paper_injects_section_rubrics():
    msgs = templates.review_paper(
        paper_text="正文", dimensions=["logic"], section_rubrics="- 《方法》：是否可复现？"
    )
    user_text = "\n".join(m.content for m in msgs)
    assert "是否可复现" in user_text
    assert "体裁" in user_text


def test_review_agent_builds_rubrics_from_outline():
    from paper_agent.agents.review_agent import ReviewAgent

    ws = PaperWorkspace(workspace_id="w", input_mode=InputMode.GENERATION)
    ws.outline.append(OutlineNode(section_id="method", title="方法", order=0))
    ws.outline.append(OutlineNode(section_id="exp", title="实验", order=1))
    rubrics = ReviewAgent._section_rubrics(ws)
    # Method 与 Experiments 都有 review_rubric，应各出现一次。
    assert "方法" in rubrics
    assert "实验" in rubrics
