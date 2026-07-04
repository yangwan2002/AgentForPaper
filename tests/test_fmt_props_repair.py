"""format-pipeline-and-diff-revision Format_Repair_Loop / 降级 / 原子写入 属性测试
（Property 21–30）。

每条 Correctness Property 用单个 Hypothesis 属性测试实现（max_examples=100），
直接驱动 ``FormatRepairLoop.run`` 的终止性 / 防御式截断 / 无效修复丢弃 / 可观测
脱敏 / 单一写路径 / 产物一致性，以及配置校验与 exporter 级降级隔离。

外部工具（pandoc / pdflatex）与 LLM 一律以确定性桩注入，从而在无真实二进制、
可重复、可低成本运行 100+ 次的前提下验证「工具为唯一裁判」「终止性」「单一写入
路径」等属性——不依赖真实 pandoc / 网络 / 磁盘（除少数需真实文件产物的降级用例）。
"""

from __future__ import annotations

import dataclasses
import tempfile

from hypothesis import assume, given, settings
from hypothesis import strategies as st

from paper_agent.config import Config
from paper_agent.export import content_contract
from paper_agent.export.base import ExportResult
from paper_agent.export.format_models import (
    FormatGateReport,
    RepairAttempt,
    RepairOutcome,
    RepairTerminalStatus,
    ToolRunResult,
)
from paper_agent.export.format_repair import FormatRepairLoop
from paper_agent.export.latex import LatexExporter
from paper_agent.export.markdown import MarkdownExporter
from paper_agent.observability.events import NullSink
from paper_agent.orchestrator import Orchestrator
from paper_agent.providers.llm.base import LLMResponse
from paper_agent.workspace.models import (
    InputMode,
    OutlineNode,
    OutputFormat,
    PaperWorkspace,
    SectionDraft,
)
from paper_agent.workspace.repository import WorkspaceRepository
from paper_agent.workspace.store import InMemoryStore, PersistenceError

_PANDOC_DEGRADE_NOTE = "已降级：pandoc 不可用"
_EXHAUSTED_NOTE_PREFIX = "格式未通过：已达修复上限"


# --------------------------------------------------------------------------- #
# 公共构造工具与桩
# --------------------------------------------------------------------------- #


def _ws(sections: dict[str, str], fmt: OutputFormat = OutputFormat.LATEX) -> PaperWorkspace:
    """构造含给定章节草稿的最小工作区（section_id -> content）。"""
    ws = PaperWorkspace(
        workspace_id="w",
        input_mode=InputMode.GENERATION,
        topic_background="x",
        output_format=fmt,
    )
    for order, (sid, content) in enumerate(sections.items()):
        ws.outline.append(OutlineNode(section_id=sid, title=sid, order=order))
        ws.section_drafts[sid] = SectionDraft(section_id=sid, title=sid, content=content)
    return ws


def _failing_report(
    fmt: OutputFormat = OutputFormat.LATEX,
    exit_code: int = 1,
    stderr: str = "conversion failed",
) -> FormatGateReport:
    """构造一个 passed=False 的格式闸报告，携带一条 pandoc 非零退出结果。"""
    return FormatGateReport(
        passed=False,
        output_format=fmt,
        tool_results=[
            ToolRunResult(tool_name="pandoc", exit_code=exit_code, stderr_excerpt=stderr)
        ],
    )


class _Resp:
    """最小 LLM 响应对象：仅暴露 ``.content``（loop 只读取该属性）。"""

    def __init__(self, content):
        self.content = content


class _StubLLM:
    """可控 LLM 桩：捕获收到的 messages，按脚本返回 ``.content``。

    - ``outputs`` 为 str：每次返回同一内容。
    - ``outputs`` 为 list：按调用序循环返回。
    - ``factory`` 为可调用：以调用序号（1..）产出内容。
    """

    def __init__(self, outputs=None, factory=None):
        self.received: list = []
        self._outputs = outputs
        self._factory = factory
        self._n = 0

    def complete(self, messages, **opts):
        self.received.append(messages)
        self._n += 1
        if self._factory is not None:
            content = self._factory(self._n)
        elif isinstance(self._outputs, list):
            content = self._outputs[(self._n - 1) % len(self._outputs)]
        else:
            content = self._outputs
        return _Resp(content)


class _ScriptedGate:
    """格式闸桩：``check(fmt, files)`` 按脚本布尔序列返回 passed；耗尽后恒 False。"""

    def __init__(self, sequence):
        self._seq = list(sequence)
        self._i = 0
        self.calls: list = []

    def check(self, fmt, files):
        self.calls.append((fmt, list(files)))
        passed = self._seq[self._i] if self._i < len(self._seq) else False
        self._i += 1
        return FormatGateReport(
            passed=passed,
            output_format=fmt,
            tool_results=[
                ToolRunResult(
                    tool_name="pandoc",
                    exit_code=0 if passed else 1,
                    stderr_excerpt="" if passed else "still failing",
                )
            ],
        )


class _StubExporter:
    """导出器桩：``export(ws, out_dir)`` 返回固定 ``ExportResult``（不触磁盘）。"""

    format = OutputFormat.LATEX

    def __init__(self, files=None):
        self.files = files if files is not None else ["out/w.tex"]
        self.calls = 0

    def export(self, ws, out_dir):
        self.calls += 1
        return ExportResult(output_format=OutputFormat.LATEX, files=list(self.files))


# 一段合规的 Normalized_Markdown（可解析、无契约外构造）。
_VALID_MD = "# Fixed section\n\nThis is repaired body text with $x^2$ math."


# --------------------------------------------------------------------------- #
# Property 21: 修复循环终止性
# --------------------------------------------------------------------------- #
# Feature: format-pipeline-and-diff-revision, Property 21: 修复循环终止性——工具链运行
# 总次数 ≤ max_repair_attempts+1 且必然终止，状态 ∈ {repaired_within_limit,
# repair_exhausted}；首次 passed==True 后即终止并采用该结果。
@settings(max_examples=100, deadline=None)
@given(
    max_attempts=st.integers(min_value=0, max_value=10),
    seq=st.lists(st.booleans(), max_size=12),
)
def test_p21_repair_loop_terminates_and_is_bounded(max_attempts, seq):
    ws = _ws({"s0": "original body", "s1": "second body"})
    llm = _StubLLM(outputs=_VALID_MD)  # 恒返回合规内容 → 每轮都写回并重校验
    gate = _ScriptedGate(seq)
    loop = FormatRepairLoop(llm, gate, max_repair_attempts=max_attempts)

    outcome = loop.run(ws, _failing_report(), _StubExporter(), "out")

    # 终止性 + 有界性（无论输入序列如何）。
    assert outcome.status in (
        RepairTerminalStatus.REPAIRED_WITHIN_LIMIT,
        RepairTerminalStatus.REPAIR_EXHAUSTED,
    )
    assert outcome.tool_runs <= max_attempts + 1

    if max_attempts == 0:
        # 不做任何尝试；状态依据传入的（失败）报告 → 已耗尽。
        assert outcome.status == RepairTerminalStatus.REPAIR_EXHAUSTED
        assert outcome.tool_runs == 1
        assert outcome.attempts == []
        return

    # 找首个使 passed==True 的重校验（0 基）落在 [0, max) 内。
    first_true = next((i for i in range(max_attempts) if i < len(seq) and seq[i]), None)
    if first_true is not None:
        expected_iters = first_true + 1
        assert outcome.status == RepairTerminalStatus.REPAIRED_WITHIN_LIMIT
    else:
        expected_iters = max_attempts
        assert outcome.status == RepairTerminalStatus.REPAIR_EXHAUSTED

    assert len(outcome.attempts) == expected_iters
    assert outcome.tool_runs == 1 + expected_iters


# --------------------------------------------------------------------------- #
# Property 22: 修复输入防御式截断
# --------------------------------------------------------------------------- #
# Feature: format-pipeline-and-diff-revision, Property 22: 修复输入防御式截断——交给 LLM
# 的工具错误片段 ≤2000 字符、出错 Markdown 片段经防御式截断 ≤8000 字符。
@settings(max_examples=100, deadline=None)
@given(
    err_len=st.integers(min_value=2001, max_value=5000),
    md_len=st.integers(min_value=8001, max_value=15000),
)
def test_p22_repair_inputs_are_defensively_truncated(err_len, md_len):
    oversized_md = "A" * md_len  # 无标记字符，便于精确切分
    oversized_err = "E" * err_len
    ws = _ws({"s0": oversized_md})
    llm = _StubLLM(outputs=_VALID_MD)
    gate = _ScriptedGate([True])
    loop = FormatRepairLoop(llm, gate, max_repair_attempts=1)

    loop.run(ws, _failing_report(stderr=oversized_err), _StubExporter(), "out")

    assert llm.received, "LLM 应至少被调用一次以获取修复候选"
    user_msg = llm.received[0][1]  # [0]=system, [1]=user
    content = user_msg.content

    # 出错 Markdown 片段 ≤8000（section_md 被 _truncate 至 8000）。
    section_md = content.split("当前 Normalized Markdown]\n", 1)[1]
    assert len(section_md) <= 8000

    # 工具错误片段 ≤2000（_extract_tool_error 汇聚后截断至 2000）。
    tool_error = content.split("[工具错误]\n", 1)[1].split("\n\n[章节 ", 1)[0]
    assert len(tool_error) <= 2000


# --------------------------------------------------------------------------- #
# Property 23: 无效修复被丢弃且章节不变
# --------------------------------------------------------------------------- #
# Feature: format-pipeline-and-diff-revision, Property 23: 无效修复被丢弃且章节不变——
# 无法解析或含契约外构造的 LLM 输出被丢弃、不写回、目标章节字节级不变、且计入尝试次数。
@settings(max_examples=100, deadline=None)
@given(
    max_attempts=st.integers(min_value=1, max_value=5),
    bad=st.sampled_from(["", "   ", "\n\t ", "<div>bad</div>", "text with <br/> tag"]),
    contents=st.lists(
        st.text(min_size=1, max_size=40), min_size=1, max_size=2, unique=True
    ),
)
def test_p23_invalid_repair_discarded_section_unchanged(max_attempts, bad, contents):
    sections = {f"s{i}": c for i, c in enumerate(contents)}
    ws = _ws(sections)
    originals = {sid: d.content for sid, d in ws.section_drafts.items()}

    llm = _StubLLM(outputs=bad)  # 无法解析 / 含契约外构造
    gate = _ScriptedGate([])  # 不应被调用（无写回）
    loop = FormatRepairLoop(llm, gate, max_repair_attempts=max_attempts)

    outcome = loop.run(ws, _failing_report(), _StubExporter(), "out")

    # 章节 content 字节级不变（全部章节）。
    for sid, before in originals.items():
        assert ws.section_drafts[sid].content == before

    # 无写回、每次均计入尝试、且状态为已耗尽。
    assert outcome.mutations == []
    assert len(outcome.attempts) == max_attempts
    assert all(a.accepted is False for a in outcome.attempts)
    assert outcome.status == RepairTerminalStatus.REPAIR_EXHAUSTED
    assert gate.calls == []  # 未发生任何重校验


# --------------------------------------------------------------------------- #
# Property 24: 修复可观测脱敏
# --------------------------------------------------------------------------- #
# Feature: format-pipeline-and-diff-revision, Property 24: 修复可观测脱敏——每次尝试记录
# 工具错误类别与尝试序号（1..max），不打印 API 密钥或完整请求体。
@settings(max_examples=100, deadline=None)
@given(
    max_attempts=st.integers(min_value=1, max_value=10),
    exit_code=st.integers(min_value=1, max_value=255),
)
def test_p24_repair_observability_is_redacted(max_attempts, exit_code):
    ws = _ws({"s0": "body"})
    # 把一段“类密钥”文本塞进工具错误，验证其不会外泄到尝试记录。
    secret = "sk-SUPERSECRETAPIKEY_should_never_leak"
    report = _failing_report(exit_code=exit_code, stderr=f"boom {secret}")

    llm = _StubLLM(outputs="")  # 无效 → 全部尝试被记录（len == max）
    gate = _ScriptedGate([])
    loop = FormatRepairLoop(llm, gate, max_repair_attempts=max_attempts)

    outcome = loop.run(ws, report, _StubExporter(), "out")

    # RepairAttempt 结构本身即已脱敏：仅这四个字段，无处承载密钥/请求体。
    attempt_fields = {f.name for f in dataclasses.fields(RepairAttempt)}
    assert attempt_fields == {"index", "tool_error_category", "accepted", "gate_passed"}

    assert len(outcome.attempts) == max_attempts
    for i, attempt in enumerate(outcome.attempts, start=1):
        assert attempt.index == i
        assert 1 <= attempt.index <= max_attempts
        # 工具错误类别只含类别信息，绝不含密钥或工具原始 stderr。
        assert attempt.tool_error_category == f"pandoc_exit_{exit_code}"
        assert secret not in attempt.tool_error_category
        assert "sk-" not in attempt.tool_error_category


# --------------------------------------------------------------------------- #
# Property 25: 修复耗尽优雅降级（经 Orchestrator 接线）
# --------------------------------------------------------------------------- #
# Feature: format-pipeline-and-diff-revision, Property 25: 修复耗尽优雅降级——repair_exhausted
# 时管线不中止、输出最近一次产物，notes 含「格式未通过：已达修复上限」及最后错误片段（≤2000）。
@settings(max_examples=100, deadline=None)
@given(
    exit_code=st.integers(min_value=1, max_value=255),
    stderr=st.text(min_size=1, max_size=3000),
)
def test_p25_repair_exhausted_graceful_degradation(exit_code, stderr):
    ws = _ws({"s0": "body"}, fmt=OutputFormat.LATEX)
    latest_files = ["out/w.tex", "out/w.bib"]
    exporter = _StubExporter(files=latest_files)
    last_report = _failing_report(exit_code=exit_code, stderr=stderr)

    class _AlwaysFailGate:
        def check(self, fmt, files):
            return FormatGateReport(
                passed=False,
                output_format=fmt,
                tool_results=[ToolRunResult("pandoc", exit_code, stderr[:2000])],
                missing_tools=[],
            )

    def _mutation(w):
        w.section_drafts["s0"].content = "repaired body"

    class _StubRepairExhausted:
        def run(self, w, report, exp, out_dir):
            return RepairOutcome(
                status=RepairTerminalStatus.REPAIR_EXHAUSTED,
                attempts=[RepairAttempt(1, f"pandoc_exit_{exit_code}", True, False)],
                tool_runs=2,
                last_report=last_report,
                mutations=[_mutation],
            )

    # 以最小属性装配一个 Orchestrator，仅驱动其格式闸/修复接线（不构造完整依赖）。
    orch = object.__new__(Orchestrator)
    orch._config = Config()
    orch._sink = NullSink()
    orch._repo = WorkspaceRepository(InMemoryStore())
    orch._format_gate = _AlwaysFailGate()
    orch._format_repair_loop = _StubRepairExhausted()

    initial = ExportResult(output_format=OutputFormat.LATEX, files=["stale.tex"])
    result = orch._run_format_gate(ws, exporter, initial)

    # 管线不中止：返回一个 ExportResult，并反映最近一次（修复后重导出）产物。
    assert isinstance(result, ExportResult)
    assert result.files == latest_files

    # notes 含一致措辞标注；且每条 note 有界（≤2000）。
    exhausted_notes = [n for n in result.notes if n.startswith(_EXHAUSTED_NOTE_PREFIX)]
    assert exhausted_notes, "应标注『格式未通过：已达修复上限』"
    assert "最后错误" in exhausted_notes[0]
    assert all(len(n) <= 2000 for n in result.notes)


# --------------------------------------------------------------------------- #
# Property 26: pandoc 不可用时降级隔离且标注一致
# --------------------------------------------------------------------------- #
# Feature: format-pipeline-and-diff-revision, Property 26: pandoc 不可用时降级隔离且标注
# 一致——fallback 策略下能手写产出的格式产出并一致标注「已降级：pandoc 不可用」，
# 且不影响其他格式；Markdown 从不降级。
@settings(max_examples=100, deadline=None)
@given(
    contents=st.lists(
        st.text(
            # 排除代理/控制字符（Cs/Cc）：孤立代理无法 UTF-8 落盘，真实正文不含它们。
            alphabet=st.characters(blacklist_categories=("Cs", "Cc")),
            min_size=0,
            max_size=60,
        ),
        min_size=1,
        max_size=2,
    )
)
def test_p26_pandoc_unavailable_isolated_and_consistent(contents):
    sections = {f"s{i}": c for i, c in enumerate(contents)}

    class _UnavailablePandoc:
        def probe(self, timeout=5.0):
            return False

        def convert(self, *args, **kwargs):  # pragma: no cover - 不应被调用
            raise AssertionError("pandoc unavailable: convert must not be called")

    with tempfile.TemporaryDirectory() as tmp:
        # LaTeX：pandoc 不可用 + fallback → 手写渲染产出，并一致标注降级。
        latex_ws = _ws(sections, fmt=OutputFormat.LATEX)
        latex_exporter = LatexExporter(pandoc=_UnavailablePandoc())
        latex_result = latex_exporter.export(latex_ws, tmp)
        assert latex_result.files, "LaTeX 应经手写渲染仍产出文件"
        assert _PANDOC_DEGRADE_NOTE in latex_result.notes

        # Markdown：从不调用 pandoc，始终成功且从不标注降级（隔离）。
        md_ws = _ws(sections, fmt=OutputFormat.MARKDOWN)
        md_result = MarkdownExporter().export(md_ws, tmp)
        assert md_result.files, "Markdown 应始终产出文件"
        assert _PANDOC_DEGRADE_NOTE not in md_result.notes


# --------------------------------------------------------------------------- #
# Property 27: 非法降级策略被拒
# --------------------------------------------------------------------------- #
# Feature: format-pipeline-and-diff-revision, Property 27: 非法降级策略被拒——不属于
# {fallback, fail_fast} 的降级策略被 Config.validate() 以 1–500 字符 ValueError 拒绝。
@settings(max_examples=100)
@given(strategy=st.text(max_size=40))
def test_p27_invalid_degrade_strategy_rejected(strategy):
    assume(strategy not in ("fallback", "fail_fast"))
    config = Config()  # 其余字段取合法默认值
    config.pandoc_degrade_strategy = strategy

    try:
        config.validate()
    except ValueError as exc:
        message = str(exc)
        assert 1 <= len(message) <= 500
        # 错误信息应指明允许的取值。
        assert "fallback" in message and "fail_fast" in message
    else:
        raise AssertionError("非法降级策略必须被 Config.validate() 拒绝")


# --------------------------------------------------------------------------- #
# Property 28: 单一原子写入路径
# --------------------------------------------------------------------------- #
# Feature: format-pipeline-and-diff-revision, Property 28: 单一原子写入路径——修复写回以
# RepairOutcome.mutations（WorkspaceMutation 可调用）承载，且仅触及目标章节。
@settings(max_examples=100, deadline=None)
@given(
    contents=st.lists(
        st.text(min_size=1, max_size=40), min_size=2, max_size=2, unique=True
    ),
)
def test_p28_single_atomic_write_path_only_target_section(contents):
    sections = {f"s{i}": c for i, c in enumerate(contents)}
    ws = _ws(sections)
    target_sid = ws.ordered_sections()[0].section_id  # loop 修复的首个章节

    llm = _StubLLM(outputs=_VALID_MD)
    gate = _ScriptedGate([True])  # 首次重校验即通过 → 恰一次写回
    loop = FormatRepairLoop(llm, gate, max_repair_attempts=3)

    outcome = loop.run(ws, _failing_report(), _StubExporter(), "out")

    # 写回以 mutations（可调用）承载。
    assert outcome.mutations, "被采纳的修复必须以 mutations 承载"
    assert all(callable(m) for m in outcome.mutations)

    expected = content_contract.normalize(_VALID_MD).content

    # 把 mutations 施加到一份全新工作区，验证仅目标章节被改动。
    fresh = _ws(sections)
    originals = {sid: d.content for sid, d in fresh.section_drafts.items()}
    for m in outcome.mutations:
        m(fresh)

    assert fresh.section_drafts[target_sid].content == expected
    for sid, before in originals.items():
        if sid == target_sid:
            continue
        assert fresh.section_drafts[sid].content == before  # 非目标章节字节不变


# --------------------------------------------------------------------------- #
# Property 29: 落盘失败原子回滚
# --------------------------------------------------------------------------- #
# Feature: format-pipeline-and-diff-revision, Property 29: 落盘失败原子回滚——落盘失败时
# WorkspaceRepository 回滚并恢复到写入前的字节级状态，不留部分写入。
@settings(max_examples=100, deadline=None)
@given(
    original=st.text(min_size=0, max_size=200),
    new_content=st.text(min_size=0, max_size=200),
)
def test_p29_persistence_failure_atomic_rollback(original, new_content):
    store = InMemoryStore()
    repo = WorkspaceRepository(store)
    ws = _ws({"s0": original})
    repo.create(ws)  # 首次落盘成功（fail_on_save 仍为 False）

    # 注入落盘失败，随后尝试写回新内容。
    store.fail_on_save = True

    def _mutate(w):
        w.section_drafts["s0"].content = new_content

    raised = False
    try:
        repo.update(ws, _mutate)
    except PersistenceError:
        raised = True

    assert raised, "落盘失败必须向上抛出 PersistenceError"
    # 字节级回滚：内存状态恢复到写入前。
    assert ws.section_drafts["s0"].content == original


# --------------------------------------------------------------------------- #
# Property 30: 修复终止后产物与工作区一致
# --------------------------------------------------------------------------- #
# Feature: format-pipeline-and-diff-revision, Property 30: 修复终止后产物与工作区一致——
# 工作区保留最后一次成功写回的章节 content，且与 RepairOutcome.mutations 施加结果一致。
@settings(max_examples=100, deadline=None)
@given(
    max_attempts=st.integers(min_value=1, max_value=6),
    pass_at=st.integers(min_value=1, max_value=6),
)
def test_p30_workspace_consistent_after_termination(max_attempts, pass_at):
    sections = {"s0": "original body", "s1": "second body"}
    ws = _ws(sections)
    target_sid = ws.ordered_sections()[0].section_id

    # 每次尝试产出可区分的合规内容，以检验“最后一次成功写回”的一致性。
    def _factory(n):
        return f"# Fixed attempt {n}\n\nbody number {n} with $a_{{{n}}}$"

    llm = _StubLLM(factory=_factory)
    # 第 pass_at 次重校验通过（若 ≤ max）；否则耗尽——两种终止路径都覆盖。
    seq = [False] * (pass_at - 1) + [True]
    gate = _ScriptedGate(seq)
    loop = FormatRepairLoop(llm, gate, max_repair_attempts=max_attempts)

    outcome = loop.run(ws, _failing_report(), _StubExporter(), "out")

    assert outcome.mutations, "至少发生一次成功写回"

    # 工作区（loop 在内存中施加的结果）与把 mutations 施加到全新工作区一致。
    fresh = _ws(sections)
    for m in outcome.mutations:
        m(fresh)
    assert fresh.section_drafts[target_sid].content == ws.section_drafts[target_sid].content

    # 且等于最后一次被采纳的归一化内容（“最后一次成功写回”）。
    accepted_indices = [a.index for a in outcome.attempts if a.accepted]
    last_idx = accepted_indices[-1]
    expected = content_contract.normalize(_factory(last_idx)).content
    assert ws.section_drafts[target_sid].content == expected
