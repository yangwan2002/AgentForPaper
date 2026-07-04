"""Property-based tests (Hypothesis) for the write-path / figure-render slice of the
`venue-templates-figures-tables` spec.

Each test implements exactly one Correctness Property from
`.kiro/specs/venue-templates-figures-tables/design.md` and is annotated with the
`# Feature: ... , Property N: ...` marker required by the design's testing strategy.

The tests use stub `PlottingBackend` implementations and fake `EventSink`s so that
they exercise real production logic (FigureRenderer, WritingAgent write path,
LatexExporter, WorkspaceRepository, truncation helpers) without any optional
matplotlib dependency.
"""

from __future__ import annotations

import copy
import os
import shutil
import tempfile

from hypothesis import given, settings
from hypothesis import strategies as st

from paper_agent.agents.base import AgentContext
from paper_agent.agents.tool_loop import _MAX_TOOL_RESULT_CHARS, truncate_to_tokens
from paper_agent.agents.writing_agent import WritingAgent
from paper_agent.context.tokenizer import build_token_counter
from paper_agent.export.figure_renderer import (
    _MAX_EVENT_CHARS,
    _MAX_FIELD_CHARS,
    FigureRenderer,
    _truncate,
)
from paper_agent.export.grounding import GroundingChecker
from paper_agent.export.latex import LatexExporter
from paper_agent.observability.events import Event, EventKind
from paper_agent.providers.llm.base import LLMResponse
from paper_agent.workspace.models import (
    FigureRecord,
    InputMode,
    OutlineNode,
    OutputFormat,
    PaperWorkspace,
    ReferenceEntry,
    SectionDraft,
)
from paper_agent.workspace.repository import WorkspaceRepository
from paper_agent.workspace.research_artifact import (
    Contribution,
    Experiment,
    MethodSpec,
    ResearchArtifact,
)


# --------------------------------------------------------------------------- #
# Test doubles                                                                #
# --------------------------------------------------------------------------- #


class FakeSink:
    """Records every emitted event for assertions."""

    def __init__(self) -> None:
        self.events: list[Event] = []

    def emit(self, event: Event) -> None:
        self.events.append(event)

    def kinds(self) -> list[EventKind]:
        return [e.kind for e in self.events]

    def degradations(self) -> list[Event]:
        return [e for e in self.events if e.kind is EventKind.DEGRADATION]


class StubBackend:
    """Stub PlottingBackend: writes a small file on bar_chart (or raises)."""

    def __init__(self, available: bool = True, raises: bool = False) -> None:
        self.available = available
        self.raises = raises
        self.calls: list[tuple] = []

    def bar_chart(self, title, labels, values, out_path) -> None:  # noqa: D401
        self.calls.append((title, tuple(labels), tuple(values), out_path))
        if self.raises:
            raise RuntimeError("simulated plotting backend failure")
        with open(out_path, "wb") as fh:
            fh.write(b"\x89PNG\r\n\x1a\n")  # tiny non-empty PNG-ish file


class StubLLM:
    """Deterministic LLM stub used only to exercise the WritingAgent write path."""

    def complete(self, messages, **opts) -> LLMResponse:  # noqa: D401
        return LLMResponse(content="Generated body text.")


class FailingStore:
    """WorkspaceStore stub whose save() always raises (to trigger rollback)."""

    def __init__(self) -> None:
        self.save_attempts = 0

    def load(self, workspace_id: str):
        return None

    def save(self, ws) -> None:  # noqa: D401
        self.save_attempts += 1
        raise RuntimeError("simulated persistence failure")


# --------------------------------------------------------------------------- #
# Data builders / strategies                                                  #
# --------------------------------------------------------------------------- #


def make_experiment(exp_id: str, metric: str, pairs: list[tuple[str, float]]) -> Experiment:
    """Build an Experiment whose results_data rows/stats make every value grounded."""
    vals = [v for _, v in pairs]
    rows = [{"method": label, metric: val} for label, val in pairs]
    stats = {
        metric: {
            "mean": sum(vals) / len(vals),
            "std": 0.0,
            "min": min(vals),
            "max": max(vals),
            "n": len(vals),
        }
    }
    return Experiment(
        experiment_id=exp_id,
        dataset="dataset",
        baselines=[label for label, _ in pairs],
        metrics=[metric],
        results_data={"columns": ["method", metric], "rows": rows, "stats": stats},
    )


def make_artifact(experiments: list[Experiment]) -> ResearchArtifact:
    return ResearchArtifact(
        research_question="q",
        method=MethodSpec(overview="overview"),
        contributions=[Contribution(summary="c1")],
        experiments=experiments,
    )


_label_st = st.text(alphabet="abcdefghijklmnopqrstuvwxyz", min_size=1, max_size=6)
_value_st = st.floats(min_value=0.1, max_value=1000.0, allow_nan=False, allow_infinity=False)
_metric_st = st.sampled_from(["accuracy", "f1", "bleu", "recall", "precision"])


@st.composite
def experiment_strategy(draw) -> Experiment:
    metric = draw(_metric_st)
    n = draw(st.integers(min_value=1, max_value=4))
    labels = draw(
        st.lists(_label_st, min_size=n, max_size=n, unique=True)
    )
    values = draw(st.lists(_value_st, min_size=n, max_size=n))
    exp_id = draw(st.text(alphabet="abcdefghijklmnopqrstuvwxyz", min_size=1, max_size=5))
    return make_experiment(exp_id, metric, list(zip(labels, values)))


@st.composite
def artifact_strategy(draw) -> ResearchArtifact:
    n = draw(st.integers(min_value=1, max_value=3))
    # Unique experiment ids so figure_ids stay unique across experiments.
    exps = []
    for i in range(n):
        exp = draw(experiment_strategy())
        exp.experiment_id = f"{exp.experiment_id}{i}"
        exps.append(exp)
    return make_artifact(exps)


# --------------------------------------------------------------------------- #
# Property 18                                                                  #
# --------------------------------------------------------------------------- #


# Feature: venue-templates-figures-tables, Property 18: 数据出图产出资产与记录（含非空
# results_data 的实验，启用且后端可用时产出 RenderedFigure，其 FigureRecord 含
# figure_id 与指向已落盘图像的 data_ref，且图像文件存在）。
@settings(max_examples=100, deadline=None)
@given(artifact=artifact_strategy())
def test_property_18_data_figures_produce_asset_and_record(artifact):
    sink = FakeSink()
    backend = StubBackend(available=True)
    renderer = FigureRenderer(
        backend=backend,
        grounding=GroundingChecker(artifact),
        sink=sink,
        tracker=None,
        enabled=True,
    )
    assets_dir = tempfile.mkdtemp(prefix="p18_")
    try:
        rendered = renderer.render_from_artifact(artifact, assets_dir)

        # Every experiment with non-empty results_data yields at least one figure.
        assert len(rendered) >= 1
        for rf in rendered:
            assert rf.record.figure_id
            assert rf.record.data_ref  # non-empty ref to the landed image
            assert rf.record.rendered_from_data is True
            assert os.path.isabs(rf.asset_path)
            assert os.path.exists(rf.asset_path)
            # data_ref (relative filename) must resolve inside the assets dir.
            assert os.path.exists(os.path.join(assets_dir, rf.record.data_ref))
    finally:
        shutil.rmtree(assets_dir, ignore_errors=True)


# --------------------------------------------------------------------------- #
# Property 19                                                                  #
# --------------------------------------------------------------------------- #


# Feature: venue-templates-figures-tables, Property 19: 单一写入路径不变式（WritingAgent
# 仅经 AgentResult.mutations 返回图写回意图；mutation 应用前 ws.figures 逐字节不变，
# 应用后含新的 FigureRecord；不绕过 WorkspaceRepository 直接写工作区）。
@settings(max_examples=100, deadline=None)
@given(artifact=artifact_strategy())
def test_property_19_single_write_path_invariant(artifact):
    workspace_dir = tempfile.mkdtemp(prefix="p19_")
    try:
        ws = PaperWorkspace(
            workspace_id="ws19",
            input_mode=InputMode.GENERATION,
            output_format=OutputFormat.MARKDOWN,
            topic_background="t",
        )
        ws.outline = [OutlineNode(section_id="intro", title="Introduction", order=0)]
        ws.artifact = artifact

        sink = FakeSink()
        renderer = FigureRenderer(
            backend=StubBackend(available=True),
            grounding=GroundingChecker(artifact),
            sink=sink,
            tracker=None,
            enabled=True,
        )
        agent = WritingAgent(
            llm=StubLLM(),
            figure_renderer=renderer,
            workspace_dir=workspace_dir,
            sink=sink,
            figures_from_data_enabled=True,
        )

        figures_before = [vars(f) for f in ws.figures]
        result = agent.run(AgentContext(workspace=ws))

        # run() must NOT have mutated the workspace figures directly.
        assert [vars(f) for f in ws.figures] == figures_before
        assert result.mutations  # write intent is carried via mutations only

        # Applying the returned mutations introduces the new FigureRecord(s).
        for mutate in result.mutations:
            mutate(ws)
        assert len(ws.figures) > len(figures_before)
        assert any(f.rendered_from_data for f in ws.figures)
    finally:
        shutil.rmtree(workspace_dir, ignore_errors=True)


# --------------------------------------------------------------------------- #
# Property 20                                                                  #
# --------------------------------------------------------------------------- #


# Feature: venue-templates-figures-tables, Property 20: 绘图禁用/依赖不可用时降级为文字
# 图题（enabled=False 或 backend.available=False → 返回 []，依赖不可用时记录一条
# missing_dependency 降级事件，管线不中止）。
@settings(max_examples=100, deadline=None)
@given(artifact=artifact_strategy(), disable=st.booleans())
def test_property_20_disabled_or_unavailable_degrades(artifact, disable):
    sink = FakeSink()
    if disable:
        # Configured off: no figures, and NO degradation event (normal shutdown).
        backend = StubBackend(available=True)
        renderer = FigureRenderer(
            backend=backend, grounding=GroundingChecker(artifact),
            sink=sink, tracker=None, enabled=False,
        )
        rendered = renderer.render_from_artifact(artifact, "unused_dir")
        assert rendered == []
        assert backend.calls == []  # never plotted
        assert sink.degradations() == []
    else:
        # Backend unavailable: no figures + one missing_dependency degradation event.
        backend = StubBackend(available=False)
        renderer = FigureRenderer(
            backend=backend, grounding=GroundingChecker(artifact),
            sink=sink, tracker=None, enabled=True,
        )
        rendered = renderer.render_from_artifact(artifact, "unused_dir")
        assert rendered == []
        assert backend.calls == []
        degradations = sink.degradations()
        assert any(
            e.data.get("reason") == "missing_dependency"
            and e.data.get("feature") == "figure_render"
            for e in degradations
        )


# --------------------------------------------------------------------------- #
# Property 21                                                                  #
# --------------------------------------------------------------------------- #


# Feature: venue-templates-figures-tables, Property 21: 续跑幂等（工作区已含数据出图的
# FigureRecord 且资产已落盘时，重跑产图逻辑不重复追加记录、不重复落盘）。
@settings(max_examples=100, deadline=None)
@given(artifact=artifact_strategy())
def test_property_21_rerun_idempotent(artifact):
    workspace_dir = tempfile.mkdtemp(prefix="p21_")
    try:
        ws = PaperWorkspace(
            workspace_id="ws21",
            input_mode=InputMode.GENERATION,
            output_format=OutputFormat.MARKDOWN,
            topic_background="t",
        )
        ws.outline = [OutlineNode(section_id="intro", title="Introduction", order=0)]
        ws.artifact = artifact

        renderer = FigureRenderer(
            backend=StubBackend(available=True),
            grounding=GroundingChecker(artifact),
            sink=FakeSink(),
            tracker=None,
            enabled=True,
        )
        agent = WritingAgent(
            llm=StubLLM(),
            figure_renderer=renderer,
            workspace_dir=workspace_dir,
            sink=FakeSink(),
            figures_from_data_enabled=True,
        )

        def apply_dedup(records):
            existing = {f.figure_id for f in ws.figures}
            for rec in records:
                if rec.figure_id in existing:
                    continue
                ws.figures.append(rec)
                existing.add(rec.figure_id)

        # First run: produce and persist records (renderer already wrote assets).
        first = agent._render_data_figures(ws)
        assert first  # at least one data figure produced
        apply_dedup(first)

        snapshot_figures = copy.deepcopy([vars(f) for f in ws.figures])
        assets_dir = f"{workspace_dir}/{ws.workspace_id}_assets"
        files_before = sorted(os.listdir(assets_dir))

        # Rerun: records already present + assets exist → no new records.
        second = agent._render_data_figures(ws)
        assert second == []
        apply_dedup(second)

        # Figure records byte-identical; no new asset files created.
        assert [vars(f) for f in ws.figures] == snapshot_figures
        assert sorted(os.listdir(assets_dir)) == files_before
    finally:
        shutil.rmtree(workspace_dir, ignore_errors=True)


# --------------------------------------------------------------------------- #
# Property 22                                                                  #
# --------------------------------------------------------------------------- #


@st.composite
def latex_ws_strategy(draw) -> ResearchArtifact | None:
    return draw(st.one_of(st.none(), artifact_strategy()))


# Feature: venue-templates-figures-tables, Property 22: 落盘路径存在性（成功导出时
# Export_Result.files 列出的每个路径都存在于文件系统中）。
@settings(max_examples=100, deadline=None)
@given(
    n_sections=st.integers(min_value=1, max_value=3),
    n_refs=st.integers(min_value=0, max_value=3),
    with_figure=st.booleans(),
    artifact=latex_ws_strategy(),
)
def test_property_22_exported_files_exist(n_sections, n_refs, with_figure, artifact):
    out_dir = tempfile.mkdtemp(prefix="p22_")
    try:
        ws = PaperWorkspace(
            workspace_id="paper22",
            input_mode=InputMode.GENERATION,
            output_format=OutputFormat.LATEX,
            topic_background="t",
        )
        ws.artifact = artifact
        ws.outline = [
            OutlineNode(section_id=f"s{i}", title=f"Section {i}", order=i)
            for i in range(n_sections)
        ]
        ws.section_drafts = {
            f"s{i}": SectionDraft(
                section_id=f"s{i}", title=f"Section {i}", content="Body text."
            )
            for i in range(n_sections)
        }
        ws.verified_references = [
            ReferenceEntry(
                id=f"ref{i}", title=f"Title {i}", authors=[f"Author {i}"],
                year=2020 + i, source_id=f"src{i}", source="arxiv", verified=True,
            )
            for i in range(n_refs)
        ]

        if with_figure:
            # Pre-create the export dir + a real asset so the figure path is emitted.
            os.makedirs(out_dir, exist_ok=True)
            asset_rel = "fig1.png"
            with open(os.path.join(out_dir, asset_rel), "wb") as fh:
                fh.write(b"\x89PNG\r\n")
            ws.figures = [
                FigureRecord(figure_id="f1", data_ref=asset_rel, caption="cap")
            ]

        result = LatexExporter().export(ws, out_dir)

        # Successful (non-aborted) export produces files, and every one exists.
        assert result.files
        for path in result.files:
            assert os.path.exists(path), f"missing exported file: {path}"
    finally:
        shutil.rmtree(out_dir, ignore_errors=True)


# --------------------------------------------------------------------------- #
# Property 23                                                                  #
# --------------------------------------------------------------------------- #


# Feature: venue-templates-figures-tables, Property 23: 落盘失败回滚（store.save 抛错时
# WorkspaceRepository.update 将工作区恢复到写入前的字节级状态，不留部分写入）。
@settings(max_examples=100, deadline=None)
@given(
    add_figures=st.integers(min_value=1, max_value=3),
    add_summary=st.text(max_size=20),
)
def test_property_23_persist_failure_rolls_back(add_figures, add_summary):
    ws = PaperWorkspace(
        workspace_id="ws23",
        input_mode=InputMode.GENERATION,
        output_format=OutputFormat.MARKDOWN,
        topic_background="t",
    )
    ws.figures = [FigureRecord(figure_id="existing", data_ref="e.png")]
    snapshot = copy.deepcopy(ws.to_dict())

    repo = WorkspaceRepository(FailingStore())

    def mutate(w: PaperWorkspace) -> None:
        for i in range(add_figures):
            w.figures.append(FigureRecord(figure_id=f"new{i}", data_ref=f"n{i}.png"))
        w.section_summaries["intro"] = add_summary

    raised = False
    try:
        repo.update(ws, mutate)
    except Exception:
        raised = True

    assert raised  # persistence failure propagates
    # Workspace is byte-identical to the pre-write snapshot (full rollback).
    assert ws.to_dict() == snapshot
    assert len(ws.figures) == 1
    assert "intro" not in ws.section_summaries


# --------------------------------------------------------------------------- #
# Property 24                                                                  #
# --------------------------------------------------------------------------- #


# Feature: venue-templates-figures-tables, Property 24: 防御式截断（可观测事件文本 ≤2000
# 字符且不含密钥/完整请求体；外部/LLM 输出 >8000 字符在解析前截断至 ≤8000）。
@settings(max_examples=100, deadline=None)
@given(
    text=st.text(max_size=6000),
    chunk=st.text(alphabet="abcdefg ", min_size=1, max_size=40),
    long_metric_len=st.integers(min_value=2500, max_value=6000),
)
def test_property_24_defensive_truncation(text, chunk, long_metric_len):
    # (a) FigureRenderer field/event truncation helper never exceeds its limit.
    assert len(_truncate(text, _MAX_FIELD_CHARS)) <= _MAX_FIELD_CHARS
    assert len(_truncate(text, _MAX_EVENT_CHARS)) <= _MAX_EVENT_CHARS
    assert _truncate(None, _MAX_EVENT_CHARS) == ""

    # (a) A real emitted observability event stays within 2000 chars and carries no
    # secret material (renderer only logs structured, truncated messages).
    huge_metric = "M" * long_metric_len
    exp = Experiment(
        experiment_id="e",
        metrics=[huge_metric],
        results_data={"rows": [{"method": "a", huge_metric: "not-a-number"}]},
    )
    artifact = make_artifact([exp])
    sink = FakeSink()
    renderer = FigureRenderer(
        backend=StubBackend(available=True),
        grounding=GroundingChecker(artifact),
        sink=sink,
        tracker=None,
        enabled=True,
    )
    rendered = renderer.render_from_artifact(artifact, "unused_dir")
    assert rendered == []  # non-numeric cell skipped, no figure produced
    for event in sink.events:
        assert len(event.message) <= _MAX_EVENT_CHARS
        assert "api_key" not in event.message.lower()
        assert "sk-" not in event.message  # no API key placeholder leaked

    # (b) External/LLM output over 8000 chars is truncated to <= 8000 before parse.
    counter = build_token_counter()
    long_text = (chunk * ((16000 // len(chunk)) + 1))
    assert len(long_text) > _MAX_TOOL_RESULT_CHARS
    note = "[truncated]"
    budget = max(1, counter.count(long_text) // 2)  # force truncation
    result = truncate_to_tokens(long_text, budget, counter, note)
    assert result.endswith(note)
    head = result[: -len(note)]
    assert len(head) <= _MAX_TOOL_RESULT_CHARS


# --------------------------------------------------------------------------- #
# Property 25                                                                  #
# --------------------------------------------------------------------------- #


# Feature: venue-templates-figures-tables, Property 25: 外部调用失败时工作区不变并降级
# （绘图后端 bar_chart 抛异常 → renderer 返回 []，记录失败原因，无致命异常）。
@settings(max_examples=100, deadline=None)
@given(artifact=artifact_strategy())
def test_property_25_backend_failure_degrades(artifact):
    assets_dir = tempfile.mkdtemp(prefix="p25_")
    try:
        sink = FakeSink()
        backend = StubBackend(available=True, raises=True)
        renderer = FigureRenderer(
            backend=backend,
            grounding=GroundingChecker(artifact),
            sink=sink,
            tracker=None,
            enabled=True,
        )

        # No fatal exception escapes; result is empty (degraded to no figures).
        rendered = renderer.render_from_artifact(artifact, assets_dir)
        assert rendered == []
        assert backend.calls  # a plot attempt was made and failed
        assert any(
            e.data.get("reason") == "render_failed" for e in sink.degradations()
        )
    finally:
        shutil.rmtree(assets_dir, ignore_errors=True)
