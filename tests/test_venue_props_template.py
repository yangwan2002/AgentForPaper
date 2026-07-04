"""Property tests for venue-templates-figures-tables (模板引擎 / 会议档案部分)。

覆盖 design.md "Correctness Properties" 中的 Property 4/5/6/7/8/9：

- Property 4: 会议档案解析一致性
- Property 5: 脚手架结构完整性
- Property 6: 样式资产引用名与落盘文件一致且受截断
- Property 7: 模板回退产出完整目标文档
- Property 8: 回退/降级标注与事件一致且恰一条
- Property 9: 回退过程不调用 LLM

每条属性用单个 Hypothesis 属性测试实现，`@settings(max_examples=100)`。
"""

from __future__ import annotations

import inspect
import os
import re
import shutil
import tempfile

from hypothesis import given, settings
from hypothesis import strategies as st

from paper_agent.export.base import ExportResult
from paper_agent.export.latex import LatexExporter
from paper_agent.export.template_engine import Scaffold, TemplateEngine
from paper_agent.export.venue_profiles import StyleAsset, VenueProfile
from paper_agent.export.venue_registry import VenueRegistry
from paper_agent.observability.events import Event, EventKind
from paper_agent.workspace.models import (
    FigureRecord,
    InputMode,
    OutlineNode,
    OutputFormat,
    PaperWorkspace,
    ReferenceEntry,
    SectionDraft,
)
from paper_agent.workspace.research_artifact import (
    Experiment,
    MethodSpec,
    ResearchArtifact,
)

# --------------------------------------------------------------------------- #
# 常量与正则
# --------------------------------------------------------------------------- #

# 逐字节固定的回退降级标注（与 template_engine._DEGRADE_NOTE 一致）。
DEGRADE_NOTE = "已降级：请求的会议模板不可用，已回退到默认模板"

FALLBACK_REASONS = {"unregistered_venue", "missing_style_asset", "invalid_profile"}

# 提取首个 \documentclass 的类名参数（可带可选 [options]）。
_DOCCLASS_RE = re.compile(r"\\documentclass(?:\[[^\]]*\])?\{([^}]*)\}")
# 提取 \usepackage 的引用名参数（可带可选 [options]）。
_USEPACKAGE_RE = re.compile(r"\\usepackage(?:\[[^\]]*\])?\{([^}]*)\}")

_REGISTERED = sorted(VenueRegistry().registered_ids())
_NON_DEFAULT = sorted(v for v in _REGISTERED if v != "default")


# --------------------------------------------------------------------------- #
# 测试替身（stubs）
# --------------------------------------------------------------------------- #


class FakeSink:
    """捕获事件的假 sink。"""

    def __init__(self) -> None:
        self.events: list[Event] = []

    def emit(self, event: Event) -> None:
        self.events.append(event)


class FakePandoc:
    """始终报告不可用的 pandoc 探针 —— 强制 LatexExporter 走手写回退渲染器。"""

    def probe(self, timeout: float = 5.0) -> bool:
        return False

    def convert(self, content, target="latex"):  # pragma: no cover - 不应被调用
        raise AssertionError("pandoc.convert 不应在 pandoc 不可用时被调用")


class CountingLLM:
    """记账 LLM 桩：任何调用都会累加计数（用于断言引擎未调用 LLM）。"""

    def __init__(self) -> None:
        self.calls = 0

    def complete(self, *args, **kwargs):  # pragma: no cover - 不应被调用
        self.calls += 1
        raise AssertionError("TemplateEngine 不应调用 LLM")


class InjectRegistry(VenueRegistry):
    """对某个指定 venue_id 注入一个"坏"档案，其余（含 default）委托给内置注册表。"""

    def __init__(self, bad_id: str, bad_profile: VenueProfile | None) -> None:
        super().__init__()
        self._bad_id = bad_id.strip().lower()
        self._bad_profile = bad_profile

    def resolve(self, venue_id):
        if isinstance(venue_id, str) and venue_id.strip().lower() == self._bad_id:
            return self._bad_profile
        return super().resolve(venue_id)


# --------------------------------------------------------------------------- #
# 辅助函数
# --------------------------------------------------------------------------- #


def _mkdtemp() -> str:
    return tempfile.mkdtemp(prefix="venue_prop_")


def _read(path: str) -> str:
    with open(path, encoding="utf-8") as fh:
        return fh.read()


def _build_ws(venue_id: str, workspace_id: str = "wsp") -> PaperWorkspace:
    """构造一个最小 LaTeX 工作区（单章节）。"""
    ws = PaperWorkspace(
        workspace_id=workspace_id,
        input_mode=InputMode.GENERATION,
        output_format=OutputFormat.LATEX,
    )
    ws.outline = [OutlineNode(section_id="s1", title="Introduction", order=0)]
    ws.section_drafts = {
        "s1": SectionDraft(section_id="s1", title="Introduction", content="Body text.")
    }
    ws.profile = {"venue_id": venue_id}
    return ws


def _build_artifact() -> ResearchArtifact:
    """构造一个含非空 stats 的实验，使 TableRenderer 产出恰一张表。"""
    exp = Experiment(
        experiment_id="main",
        dataset="D",
        baselines=["ours", "base"],
        metrics=["acc"],
        results_data={
            "columns": ["method", "acc"],
            "rows": [
                {"method": "ours", "acc": 0.9},
                {"method": "base", "acc": 0.8},
            ],
            "stats": {"acc": {"mean": 0.85, "std": 0.05, "min": 0.8, "max": 0.9}},
        },
    )
    return ResearchArtifact(
        research_question="q",
        method=MethodSpec(overview="o"),
        contributions=[],
        experiments=[exp],
    )


def _build_rich_ws(venue_id: str, workspace_id: str) -> PaperWorkspace:
    """构造含章节/图/表/参考文献的工作区，用于内容完整性对比。"""
    ws = _build_ws(venue_id, workspace_id)
    ws.outline = [
        OutlineNode(section_id="s1", title="Introduction", order=0),
        OutlineNode(section_id="s2", title="Method", order=1),
    ]
    ws.section_drafts = {
        "s1": SectionDraft(section_id="s1", title="Introduction", content="Intro body."),
        "s2": SectionDraft(section_id="s2", title="Method", content="Method body."),
    }
    ws.figures = [
        FigureRecord(figure_id="fig1", data_ref="asset_one.png", caption="Fig one"),
        FigureRecord(figure_id="fig2", data_ref="asset_two.png", caption="Fig two"),
    ]
    ws.verified_references = [
        ReferenceEntry(
            id="r1", title="Ref One", authors=["Alice Smith"], year=2020,
            source_id="doi:1", source="x", verified=True,
        ),
        ReferenceEntry(
            id="r2", title="Ref Two", authors=["Bob Jones"], year=2021,
            source_id="doi:2", source="x", verified=True,
        ),
    ]
    ws.artifact = _build_artifact()
    return ws


def _content_counts(tex: str, bib: str) -> tuple[int, int, int, int]:
    """(章节数, 图数, 表数, 文献数) —— 内容完整性对比指标。"""
    return (
        tex.count(r"\section{"),
        tex.count(r"\begin{figure}"),
        tex.count(r"\begin{tabular}"),
        bib.count("@article{"),
    )


def _export_latex(
    ws: PaperWorkspace, out_dir: str, template_engine=None
) -> ExportResult:
    exporter = LatexExporter(
        template_engine=template_engine, pandoc=FakePandoc(), sink=FakeSink()
    )
    return exporter.export(ws, out_dir)


def _make_styles_dir(profile: VenueProfile) -> str | None:
    """为需要用户文件的 profile 造一个 styles_dir，写入所需的 stub 样式文件。

    对每个 ``requires_file`` 且声明了 ``filename`` 的资产，落一个占位文件，
    使 build_scaffold 能解析到用户样式文件、走非降级路径。无此类资产时返回 ``None``。
    """
    needs = [
        a for a in profile.style_assets if a.requires_file and a.filename
    ]
    if not needs:
        return None
    d = _mkdtemp()
    for a in needs:
        with open(os.path.join(d, a.filename), "w", encoding="utf-8") as fh:
            fh.write("% stub style asset\n")
    return d


# --------------------------------------------------------------------------- #
# Property 4: 会议档案解析一致性
# --------------------------------------------------------------------------- #


# Feature: venue-templates-figures-tables, Property 4: 会议档案解析一致性——注册 id resolve 一致且 .tex 首个 documentclass 等于 profile.document_class
@settings(max_examples=100)
@given(venue_id=st.sampled_from(_REGISTERED))
def test_property4_venue_profile_resolution_consistency(venue_id: str) -> None:
    registry = VenueRegistry()
    profile = registry.resolve(venue_id)

    # 已注册 id：resolve 返回档案，且其 venue_id 等于该 id。
    assert profile is not None
    assert profile.venue_id == venue_id

    # 非 default 已注册档案：提供所需样式文件后，导出 .tex 首个 \documentclass
    # 参数 == profile.document_class（用户样式文件齐备时不降级）。
    if venue_id != "default":
        out_dir = _mkdtemp()
        styles_dir = _make_styles_dir(profile)
        try:
            ws = _build_ws(venue_id, workspace_id="wsp4")
            if styles_dir is not None:
                ws.profile["styles_dir"] = styles_dir
            result = _export_latex(ws, out_dir)
            assert result.files, "非中止导出应产出文件"
            tex = _read(os.path.join(out_dir, f"{ws.workspace_id}.tex"))
            match = _DOCCLASS_RE.search(tex)
            assert match is not None, "导出的 .tex 应含 \\documentclass"
            assert match.group(1) == profile.document_class
        finally:
            shutil.rmtree(out_dir, ignore_errors=True)
            if styles_dir is not None:
                shutil.rmtree(styles_dir, ignore_errors=True)


# --------------------------------------------------------------------------- #
# Property 5: 脚手架结构完整性
# --------------------------------------------------------------------------- #


# Feature: venue-templates-figures-tables, Property 5: 脚手架结构完整性——包含 documentclass + 每个 usepackage 资产引用 + 必需结构
@settings(max_examples=100)
@given(venue_id=st.sampled_from(_REGISTERED))
def test_property5_scaffold_structural_completeness(venue_id: str) -> None:
    registry = VenueRegistry()
    profile = registry.resolve(venue_id)
    assert profile is not None

    engine = TemplateEngine(registry, FakeSink())
    out_dir = _mkdtemp()
    # 提供所需的用户样式文件，使需要文件的会议走非降级路径。
    styles_dir = _make_styles_dir(profile)
    try:
        scaffold = engine.build_scaffold(venue_id, out_dir, styles_dir=styles_dir)
    finally:
        shutil.rmtree(out_dir, ignore_errors=True)
        if styles_dir is not None:
            shutil.rmtree(styles_dir, ignore_errors=True)

    assert not scaffold.aborted
    assert not scaffold.degraded

    # 文档类声明。
    doc_lines = [
        line for line in scaffold.preamble_lines if line.startswith(r"\documentclass")
    ]
    assert doc_lines, "脚手架前导应含 \\documentclass 行"
    assert profile.document_class in doc_lines[0]
    assert scaffold.document_class == profile.document_class

    # 前导中：usepackage==True 的资产以其（无扩展名）name 引用出现；
    # usepackage==False（.cls 文档类，如 IEEEtran）不得发 \usepackage。
    preamble = "\n".join(scaffold.preamble_lines)
    for asset in profile.style_assets:
        ref = asset.name[:500]
        if asset.usepackage:
            assert rf"\usepackage{{{ref}}}" in preamble or ref in preamble
        else:
            assert rf"\usepackage{{{ref}}}" not in preamble

    # 必需结构 notion：至少含标题/作者/正文。
    assert {"title", "authors", "body"}.issubset(set(profile.required_structure))


# --------------------------------------------------------------------------- #
# Property 6: 样式资产引用名与落盘文件一致且受截断
# --------------------------------------------------------------------------- #


# Feature: venue-templates-figures-tables, Property 6: 样式资产按 filename 落盘于导出目录，usepackage 引用名用无扩展名 name 且 <= 500 字符
@settings(max_examples=100)
@given(long_name=st.text(min_size=501, max_size=1200))
def test_property6_style_asset_reference_and_truncation(long_name: str) -> None:
    src_dir = _mkdtemp()
    out_dir = _mkdtemp()
    try:
        # 带内置文件的 StyleAsset（真实临时 .sty），requires_file=True、usepackage=True。
        sty_path = os.path.join(src_dir, "custom_style.sty")
        with open(sty_path, "w", encoding="utf-8") as fh:
            fh.write("% style asset\n")

        builtin = StyleAsset(
            name="custom_style",
            filename="custom_style.sty",
            builtin_path=sty_path,
            kind="sty",
            requires_file=True,
            usepackage=True,
        )
        # 仅引用声明的超长 usepackage 引用名资产（测试 500 字符截断）。
        ref_only = StyleAsset(
            name=long_name, builtin_path=None, kind="sty",
            requires_file=False, usepackage=True,
        )

        profile = VenueProfile(
            venue_id="tmpvenue",
            document_class="article",
            required_structure=["title", "authors", "body"],
            style_assets=[builtin, ref_only],
        )
        registry = InjectRegistry("tmpvenue", profile)
        engine = TemplateEngine(registry, FakeSink())

        scaffold = engine.build_scaffold("tmpvenue", out_dir)

        assert not scaffold.aborted
        assert not scaffold.degraded

        # 内置资产按 filename 落盘于导出目录、存在、出现在 asset_files 中。
        assert len(scaffold.asset_files) == 1
        landed = scaffold.asset_files[0]
        assert os.path.exists(landed)
        assert os.path.abspath(landed).startswith(os.path.abspath(out_dir) + os.sep)
        assert os.path.basename(landed) == "custom_style.sty"

        # .tex 中该资产的 \usepackage 引用名用无扩展名 name（非 filename）。
        assert r"\usepackage{custom_style}" in scaffold.preamble_lines

        # 超长引用名被截断至 500，且逐字节写入的引用名 == long_name[:500]。
        expected_line = r"\usepackage{" + long_name[:500] + "}"
        assert expected_line in scaffold.preamble_lines

        # 所有 \usepackage 引用名长度 <= 500。
        preamble = "\n".join(scaffold.preamble_lines)
        for ref in _USEPACKAGE_RE.findall(preamble):
            assert len(ref) <= 500
    finally:
        shutil.rmtree(src_dir, ignore_errors=True)
        shutil.rmtree(out_dir, ignore_errors=True)


# --------------------------------------------------------------------------- #
# Property 7: 模板回退产出完整目标文档
# --------------------------------------------------------------------------- #


def _fallback_engine_for(trigger: str, rid: str):
    """为给定回退触发类型构造 (template_engine | None)。

    unregistered → None（默认 TemplateEngine + 内置注册表即可触发未注册回退）。
    missing_asset / invalid → 注入坏档案的 InjectRegistry。
    """
    if trigger == "unregistered":
        return None
    if trigger == "missing_asset":
        ghost = os.path.join(tempfile.gettempdir(), f"definitely_missing_{rid}.sty")
        profile = VenueProfile(
            venue_id=rid,
            document_class="someclass",
            required_structure=["title", "authors", "body"],
            style_assets=[StyleAsset(name="ghost.sty", builtin_path=ghost, kind="sty")],
        )
    else:  # invalid
        profile = VenueProfile(venue_id=rid, document_class="", required_structure=[])
    return TemplateEngine(InjectRegistry(rid, profile), FakeSink())


# Feature: venue-templates-figures-tables, Property 7: 触发不可用条件时回退 default 且内容(章节/图/表/文献)与直接 default 导出逐一相同
@settings(max_examples=100)
@given(
    trigger=st.sampled_from(["unregistered", "missing_asset", "invalid"]),
    suffix=st.text(alphabet="abcdefghijklmnopqrstuvwxyz0123456789", min_size=1, max_size=12),
)
def test_property7_fallback_produces_complete_default_document(
    trigger: str, suffix: str
) -> None:
    rid = "zzz" + suffix  # 保证不与内置 id 冲突

    out_fb = _mkdtemp()
    out_def = _mkdtemp()
    try:
        # 回退导出。
        ws_fb = _build_rich_ws(rid, workspace_id="wsp7fb")
        engine = _fallback_engine_for(trigger, rid)
        result_fb = _export_latex(ws_fb, out_fb, template_engine=engine)

        # 回退未中止且带逐字节固定降级标注。
        assert result_fb.files, "回退（default 可用）不应中止导出"
        assert DEGRADE_NOTE in result_fb.notes

        tex_fb = _read(os.path.join(out_fb, "wsp7fb.tex"))
        bib_fb = _read(os.path.join(out_fb, "wsp7fb.bib"))

        # 直接以 default 导出。
        ws_def = _build_rich_ws("default", workspace_id="wsp7def")
        _export_latex(ws_def, out_def)
        tex_def = _read(os.path.join(out_def, "wsp7def.tex"))
        bib_def = _read(os.path.join(out_def, "wsp7def.bib"))

        # 内容单元（章节/图/表/文献）数量逐一相同。
        assert _content_counts(tex_fb, bib_fb) == _content_counts(tex_def, bib_def)

        # 回退版本的文档类回退为 default 的 "article"。
        match = _DOCCLASS_RE.search(tex_fb)
        assert match is not None and match.group(1) == "article"
    finally:
        shutil.rmtree(out_fb, ignore_errors=True)
        shutil.rmtree(out_def, ignore_errors=True)


# --------------------------------------------------------------------------- #
# Property 8: 回退/降级标注与事件一致且恰一条
# --------------------------------------------------------------------------- #


# Feature: venue-templates-figures-tables, Property 8: 回退时 degrade_note 逐字节固定，DEGRADATION 事件恰一条且 venue_id/reason 一致
@settings(max_examples=100)
@given(
    trigger=st.sampled_from(["unregistered", "missing_asset", "invalid"]),
    suffix=st.text(alphabet="abcdefghijklmnopqrstuvwxyz0123456789", min_size=1, max_size=12),
)
def test_property8_degradation_note_and_event_consistency(
    trigger: str, suffix: str
) -> None:
    rid = "zzz" + suffix

    if trigger == "unregistered":
        registry: VenueRegistry = VenueRegistry()
        expected_reason = "unregistered_venue"
    elif trigger == "missing_asset":
        ghost = os.path.join(tempfile.gettempdir(), f"definitely_missing_{rid}.sty")
        profile = VenueProfile(
            venue_id=rid,
            document_class="someclass",
            required_structure=["title", "authors", "body"],
            style_assets=[StyleAsset(name="ghost.sty", builtin_path=ghost, kind="sty")],
        )
        registry = InjectRegistry(rid, profile)
        expected_reason = "missing_style_asset"
    else:  # invalid
        profile = VenueProfile(venue_id=rid, document_class="", required_structure=[])
        registry = InjectRegistry(rid, profile)
        expected_reason = "invalid_profile"

    sink = FakeSink()
    engine = TemplateEngine(registry, sink)
    out_dir = _mkdtemp()
    try:
        scaffold = engine.build_scaffold(rid, out_dir)
    finally:
        shutil.rmtree(out_dir, ignore_errors=True)

    # 回退发生、目标恒为 default、未中止。
    assert scaffold.degraded is True
    assert scaffold.aborted is False
    assert scaffold.document_class == "article"

    # 逐字节固定降级文本 + 枚举回退原因。
    assert scaffold.degrade_note == DEGRADE_NOTE
    assert scaffold.fallback_reason == expected_reason
    assert scaffold.fallback_reason in FALLBACK_REASONS

    # 恰一条 DEGRADATION 事件，文本/venue_id/reason 一致。
    degr = [e for e in sink.events if e.kind == EventKind.DEGRADATION]
    assert len(degr) == 1
    event = degr[0]
    assert event.message == DEGRADE_NOTE
    assert event.data.get("feature") == "template"
    assert event.data.get("venue_id") == rid
    assert event.data.get("reason") == expected_reason
    assert event.data.get("reason") in FALLBACK_REASONS


# --------------------------------------------------------------------------- #
# Property 9: 回退过程不调用 LLM
# --------------------------------------------------------------------------- #


# Feature: venue-templates-figures-tables, Property 9: 回退与脚手架产出全程不调用任何 LLMProvider
@settings(max_examples=100)
@given(
    suffix=st.text(alphabet="abcdefghijklmnopqrstuvwxyz0123456789", min_size=1, max_size=12)
)
def test_property9_fallback_does_not_call_llm(suffix: str) -> None:
    rid = "zzz" + suffix  # 未注册 → 触发回退

    # TemplateEngine 构造签名仅 registry + sink，无 LLM 依赖。
    params = list(inspect.signature(TemplateEngine.__init__).parameters)
    assert params == ["self", "registry", "sink"]

    counting = CountingLLM()  # 记账 LLM，绝不应被引擎触及
    engine = TemplateEngine(VenueRegistry(), FakeSink())

    # 引擎实例不持有任何 LLM 依赖。
    assert not any("llm" in name.lower() for name in vars(engine))
    assert counting not in vars(engine).values()

    out_dir = _mkdtemp()
    try:
        scaffold = engine.build_scaffold(rid, out_dir)
    finally:
        shutil.rmtree(out_dir, ignore_errors=True)

    # 回退确实发生，且回退过程未调用记账 LLM。
    assert scaffold.degraded is True
    assert counting.calls == 0
