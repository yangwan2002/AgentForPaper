"""第三轮修复的回归测试（#1/#2/#3/#6/#8 等）。"""

from __future__ import annotations

import tempfile
from types import SimpleNamespace

import pytest

from paper_agent.config import Config
from paper_agent.export.latex import LatexExporter
from paper_agent.export.markdown import MarkdownExporter
from paper_agent.observability.usage import UsageTracker
from paper_agent.orchestrator import Orchestrator, PaperRequest
from paper_agent.providers.llm.base import LLMError, Message
from paper_agent.providers.llm.resilient import ResilientLLMProvider
from paper_agent.workspace.models import (
    InputMode,
    OutputFormat,
    OutlineNode,
    PaperWorkspace,
    ReferenceEntry,
    SectionDraft,
)
from paper_agent.workspace.repository import WorkspaceRepository
from paper_agent.workspace.store import InMemoryStore


# --------------------------------------------------------------------------- #
# #1：流式 complete 已产出增量后不重试（避免重复输出）
# --------------------------------------------------------------------------- #


class _StreamThenFail:
    """调用 on_delta 推一段增量后抛可重试错误。"""

    def __init__(self) -> None:
        self.calls = 0
        self.delta_calls = 0

    def complete(self, messages, **opts):
        self.calls += 1
        on_delta = opts.get("on_delta")
        if on_delta is not None:
            on_delta("content", "chunk1")
            self.delta_calls += 1
        raise ConnectionError("mid-stream drop")


def test_streaming_complete_does_not_retry_after_delta_produced():
    base = _StreamThenFail()
    resilient = ResilientLLMProvider(
        base, __import__(
            "paper_agent.workspace.models", fromlist=["RetryPolicy"]
        ).RetryPolicy(max_retries=3, base_backoff=0.0, jitter=0.0)
    )
    with pytest.raises(LLMError):
        resilient.complete(
            [Message("user", "hi")], on_delta=lambda kind, text: None
        )
    # 已产出增量 → 不重试，底层只调一次。
    assert base.calls == 1
    assert base.delta_calls == 1


# --------------------------------------------------------------------------- #
# #3：OpenAICompatibleProvider 原生 stream()
# --------------------------------------------------------------------------- #


def test_openai_compatible_stream_yields_content_chunks():
    from paper_agent.providers.llm.openai_compatible import OpenAICompatibleProvider

    def _chunk(content):
        return SimpleNamespace(
            choices=[
                SimpleNamespace(
                    delta=SimpleNamespace(
                        content=content, reasoning_content=None
                    )
                )
            ]
        )

    provider = OpenAICompatibleProvider(
        model="m", api_key="k", base_url="http://local"
    )
    provider._client = SimpleNamespace(
        chat=SimpleNamespace(
            completions=SimpleNamespace(
                create=lambda **kw: iter([_chunk("hello"), _chunk(" world")])
            )
        )
    )
    chunks = list(provider.stream([Message("user", "hi")]))
    assert "".join(c.text for c in chunks if c.kind == "content") == "hello world"


# --------------------------------------------------------------------------- #
# #2：导出器把正文内 [id] 就地转换为引用记号
# --------------------------------------------------------------------------- #


def _ws_with_inline_citation() -> PaperWorkspace:
    ws = PaperWorkspace(
        workspace_id="p", input_mode=InputMode.GENERATION,
        output_format=OutputFormat.LATEX, topic_background="x",
    )
    ws.outline = [OutlineNode(section_id="intro", title="Introduction", order=0)]
    ws.verified_references = [
        ReferenceEntry(
            id="arxiv:1706.03762", title="Attention Is All You Need",
            authors=["Ashish Vaswani"], year=2017,
            source_id="1706.03762", source="arxiv", verified=True,
        )
    ]
    ws.section_drafts = {
        "intro": SectionDraft(
            section_id="intro", title="Introduction",
            content="We use attention [arxiv:1706.03762] for sequence modeling.",
            cited_reference_ids=["arxiv:1706.03762"],
        )
    }
    return ws


def test_markdown_export_preserves_inline_citation(tmp_path):
    # format-pipeline-and-diff-revision Req 7.6：Markdown 导出属于 Normalized_Markdown
    # 直接渲染，正文中的方括号引用 [id] 必须**原样保留、不改写**（取代此前
    # [id]→[n] 的内联转换行为）；编号映射改在末尾「参考文献」段体现。
    result = MarkdownExporter().export(_ws_with_inline_citation(), str(tmp_path))
    text = open(result.files[0], encoding="utf-8").read()
    assert "[arxiv:1706.03762]" in text  # 正文 [id] 原样保留（不转义/不改写）
    # 参考文献段仍含编号条目（1. ...），提供 id→编号映射。
    assert "1. " in text and "Attention Is All You Need" in text


def test_latex_export_converts_inline_citation(tmp_path):
    result = LatexExporter().export(_ws_with_inline_citation(), str(tmp_path))
    tex = next(f for f in result.files if f.endswith(".tex"))
    text = open(tex, encoding="utf-8").read()
    assert r"\cite{Vaswani2017}" in text  # 内联 \cite{key}
    assert "[arxiv:1706.03762]" not in text


def test_latex_export_falls_back_to_section_end_when_no_inline_marker(tmp_path):
    r"""正文无 [id] 标注时，仍走章节末 \cite 回退（不漏引）。"""
    ws = _ws_with_inline_citation()
    ws.section_drafts["intro"].content = "Plain text without inline marker."
    result = LatexExporter().export(ws, str(tmp_path))
    tex = next(f for f in result.files if f.endswith(".tex"))
    text = open(tex, encoding="utf-8").read()
    assert r"\cite{Vaswani2017}" in text  # 回退到章节末


# --------------------------------------------------------------------------- #
# #6：arxiv id 路由优先走 arxiv 源
# --------------------------------------------------------------------------- #


def test_retrieval_route_arxiv_id_to_arxiv_first():
    from paper_agent.providers.retrieval.api import (
        ApiRetrievalProvider,
        ArxivRetrievalProvider,
        OpenAlexRetrievalProvider,
    )

    provider = ApiRetrievalProvider()
    order = provider._route_order("1706.03762")
    assert isinstance(order[0], ArxivRetrievalProvider)
    # DOI 形态仍按默认顺序（OpenAlex 优先）。
    order_doi = provider._route_order("10.1234/foo.bar")
    assert isinstance(order_doi[0], OpenAlexRetrievalProvider)


# --------------------------------------------------------------------------- #
# #8：检索阶段完成后置位 retrieval_completed，续跑不重做
# --------------------------------------------------------------------------- #


class _RecordingSearch:
    name = "search"

    def __init__(self) -> None:
        self.calls = 0

    def run(self, ctx):
        self.calls += 1
        return AgentResult()


from paper_agent.agents.base import AgentContext, AgentResult  # noqa: E402


class _Plan1:
    name = "plan"

    def run(self, ctx):
        def mut(w):
            w.outline = [OutlineNode(section_id="rw", title="相关工作", order=0)]
            from paper_agent.workspace.models import TaskItem
            w.task_checklist = [
                TaskItem(id="t_rw", description="撰写：相关工作",
                         section_ref="rw", needs_retrieval=True)
            ]

        return AgentResult(mutations=[mut])


class _Noop:
    name = "noop"

    def run(self, ctx):
        return AgentResult()


def test_retrieval_phase_sets_completed_flag_and_runs_once():
    search = _RecordingSearch()
    repo = WorkspaceRepository(InMemoryStore())
    cfg = Config(workspace_dir=tempfile.mkdtemp(), iteration_limit=1)
    orch = Orchestrator(
        repo=repo,
        plan_agent=_Plan1(),
        search_agent=search,
        writing_agent=_Noop(),
        review_agent=_Noop(),
        config=cfg,
    )
    result = orch.run(PaperRequest(topic_background="t"))
    ws = repo.load(result.workspace_id)
    assert ws.retrieval_completed is True
    assert search.calls == 1  # 仅一次
