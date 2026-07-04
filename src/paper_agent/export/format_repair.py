"""Format_Repair_Loop：有界的、以工具链为裁判的格式修复循环。

设计文档 format-pipeline-and-diff-revision（Components/`Format_Repair_Loop`，
Property 21/23）：格式闸报错后，本循环把**工具错误**（≤2000 字符）与**出错章节
的 Normalized_Markdown 片段**（防御式截断 ≤8000 字符）交给 LLM，请其产出修复后的
Normalized_Markdown（Req 10.1）；经 `Content_Contract`（normalize + validate）
校验（Req 10.6），合规则**仅经 `AgentResult.mutations`** 写回对应章节 `content`
（Req 10.5），随后重新导出并再次运行 `Format_Gate.check`（Req 10.2）。

关键不变式与约束：

- 受 `max_repair_attempts ∈ [0, 10]` 约束；工具链运行总次数（``tool_runs``）恒
  ``≤ max_repair_attempts + 1``：初始一次由调用方完成（体现为传入的 ``report``），
  循环内每成功写回一次并重新校验才 +1（Req 10.3 / 11.6，Property 21）。
- 首次重校验 ``passed == True`` 立即终止并采用该结果，状态
  ``REPAIRED_WITHIN_LIMIT``（Req 10.4）。
- LLM 抛错 / 超时（Req 10.7）或输出**无法解析 / 含契约外构造**（``unknown_construct``，
  Req 10.6）→ 丢弃该次输出、**不写回**、目标章节 ``content`` 字节级不变、计入尝试
  次数（Property 23）。
- 每次尝试记录**工具错误类别**与**尝试序号**（1..max）；**绝不记录 API 密钥 /
  完整请求体**（Req 10.8）。
- 耗尽而未通过 → 状态 ``REPAIR_EXHAUSTED``（Req 11.1）。
- ``max_repair_attempts == 0`` → 不做任何尝试，状态依据传入的 ``report`` 判定。

外部工具与 LLM 输出一律视为不可信数据：不 ``eval`` / ``exec``，对错误文本与章节
片段做防御式截断。写回严格走「更新意图」路径——本循环把每次被采纳的写回构造为一个
``WorkspaceMutation`` 记入 ``RepairOutcome.mutations`` 供 Orchestrator 经
``WorkspaceRepository`` 持久化（单一写路径，Req 10.5 / 12.1），同时**在内存中**对
``ws`` 施加同一变更，使随后的重新导出 / 重校验能反映修复结果。

_Requirements: 10.1, 10.2, 10.3, 10.4, 10.5, 10.6, 10.7, 10.8, 11.1, 11.6_
"""

from __future__ import annotations

from paper_agent.agents.base import WorkspaceMutation
from paper_agent.export import content_contract
from paper_agent.export.format_models import (
    FormatGateReport,
    RepairAttempt,
    RepairOutcome,
    RepairTerminalStatus,
)
from paper_agent.providers.llm.base import Message
from paper_agent.workspace.models import PaperWorkspace, SectionDraft

# 交给 LLM 的工具错误文本上限（Req 10.1）。
_TOOL_ERROR_MAX = 2000

# 交给 LLM 的章节 Normalized_Markdown 片段防御式上限（Req 10.1）。
_SECTION_MD_MAX = 8000

# max_repair_attempts 的合法区间（Req 11.6，与需求 / 设计一致）。
_ATTEMPTS_MIN = 0
_ATTEMPTS_MAX = 10


def _clamp_attempts(value: int) -> int:
    """把 ``max_repair_attempts`` 收敛到 [0, 10]（防御式，Req 11.6）。"""

    try:
        v = int(value)
    except (TypeError, ValueError):
        return 3
    if v < _ATTEMPTS_MIN:
        return _ATTEMPTS_MIN
    if v > _ATTEMPTS_MAX:
        return _ATTEMPTS_MAX
    return v


def _truncate(text: str, limit: int) -> str:
    """把不可信文本防御式截断至 ``limit`` 字符。"""

    if not text:
        return ""
    return text if len(text) <= limit else text[:limit]


def _make_mutation(section_id: str, new_content: str) -> WorkspaceMutation:
    """构造把某章节 ``content`` 替换为 ``new_content`` 的工作区更新意图。

    仅经此「更新意图」写回（Req 10.5）；对不存在的章节静默跳过（防御式）。
    """

    def _mutate(ws: PaperWorkspace) -> None:
        draft = ws.section_drafts.get(section_id)
        if draft is not None:
            draft.content = new_content

    return _mutate


class FormatRepairLoop:
    """有界格式修复循环；工具退出码是唯一裁判，LLM 只负责产出候选修复。"""

    def __init__(self, llm, gate, max_repair_attempts: int = 3) -> None:
        self._llm = llm
        self._gate = gate
        self._max_repair_attempts = _clamp_attempts(max_repair_attempts)

    # ------------------------------------------------------------------ #
    # 公共 API
    # ------------------------------------------------------------------ #

    def run(
        self,
        ws: PaperWorkspace,
        report: FormatGateReport,
        exporter,
        out_dir: str,
    ) -> RepairOutcome:
        """驱动有界修复循环并返回 :class:`RepairOutcome`（Req 10.1–11.6）。

        ``report`` 为调用方已运行一次格式闸的结果（初始那次工具链运行计入
        ``tool_runs`` 基数 1）。循环内每成功写回并重新校验一次才 +1，故
        ``tool_runs ≤ max_repair_attempts + 1``（Property 21）。
        """

        attempts: list[RepairAttempt] = []
        mutations: list[WorkspaceMutation] = []
        # 初始一次格式闸运行由调用方完成，计入总次数（Req 11.6 的 “+1”）。
        tool_runs = 1
        current_report = report

        # max_repair_attempts == 0：不做任何尝试，状态依据传入报告判定。
        if self._max_repair_attempts <= 0:
            return RepairOutcome(
                status=self._status_from_report(current_report),
                attempts=attempts,
                tool_runs=tool_runs,
                last_report=current_report,
                mutations=mutations,
            )

        for index in range(1, self._max_repair_attempts + 1):
            section = self._offending_section(ws, current_report)
            if section is None:
                # 没有可定位 / 可修复的章节：无法继续，优雅终止（Req 11.1）。
                break

            tool_error = self._extract_tool_error(current_report)
            category = self._error_category(current_report)
            original_content = section.content or ""
            section_md = _truncate(original_content, _SECTION_MD_MAX)

            # --- 请 LLM 产出修复后的 Normalized_Markdown（Req 10.1）---
            try:
                corrected = self._request_repair(tool_error, section_md, section)
            except Exception:  # noqa: BLE001 - LLM 抛错/超时：丢弃、不写回、计数（Req 10.7）
                attempts.append(
                    RepairAttempt(
                        index=index,
                        tool_error_category=category,
                        accepted=False,
                        gate_passed=False,
                    )
                )
                continue

            # --- 经 Content_Contract 校验（Req 10.6）---
            if not self._is_acceptable(corrected, ws):
                # 无法解析 / 含契约外构造：丢弃、章节字节级不变、计数（Property 23）。
                attempts.append(
                    RepairAttempt(
                        index=index,
                        tool_error_category=category,
                        accepted=False,
                        gate_passed=False,
                    )
                )
                continue

            normalized = content_contract.normalize(corrected).content

            # --- 仅经更新意图写回（Req 10.5）；同时在内存中应用以便重新导出 ---
            mutation = _make_mutation(section.section_id, normalized)
            mutation(ws)
            mutations.append(mutation)

            # --- 重新导出并重新运行格式闸（Req 10.2）---
            try:
                export_result = exporter.export(ws, out_dir)
                artifact_paths = list(getattr(export_result, "files", []) or [])
                new_report = self._gate.check(ws.output_format, artifact_paths)
            except Exception:  # noqa: BLE001 - 导出/校验异常：视为本次未通过，计数后继续
                tool_runs += 1
                current_report = current_report  # 保留最近可用报告
                attempts.append(
                    RepairAttempt(
                        index=index,
                        tool_error_category=category,
                        accepted=True,
                        gate_passed=False,
                    )
                )
                continue

            tool_runs += 1
            current_report = new_report
            passed = bool(getattr(new_report, "passed", False))
            attempts.append(
                RepairAttempt(
                    index=index,
                    tool_error_category=category,
                    accepted=True,
                    gate_passed=passed,
                )
            )

            if passed:
                # 首次通过即终止并采用该结果（Req 10.4）。
                return RepairOutcome(
                    status=RepairTerminalStatus.REPAIRED_WITHIN_LIMIT,
                    attempts=attempts,
                    tool_runs=tool_runs,
                    last_report=new_report,
                    mutations=mutations,
                )

        # 尝试耗尽仍未通过（或无可修复章节）：优雅降级（Req 11.1）。
        return RepairOutcome(
            status=self._status_from_report(current_report),
            attempts=attempts,
            tool_runs=tool_runs,
            last_report=current_report,
            mutations=mutations,
        )

    # ------------------------------------------------------------------ #
    # 内部：LLM 交互（脱敏，Req 10.8）
    # ------------------------------------------------------------------ #

    def _request_repair(
        self, tool_error: str, section_md: str, section: SectionDraft
    ) -> str | None:
        """请 LLM 依据工具错误与章节片段产出修复后的 Normalized_Markdown。

        使用简单的 ``llm.complete`` 单轮调用（自包含、便于桩替换）。仅传入错误
        文本与章节 Markdown，绝不携带密钥或完整请求体（Req 10.8）。
        """

        messages = [
            Message(
                role="system",
                content=(
                    "你是学术论文的格式修复助手。下面给出外部排版工具（pandoc / "
                    "pdflatex）对某一章节报告的错误，以及该章节当前的 Normalized "
                    "Markdown。请仅输出**修复后的该章节 Normalized Markdown 正文**，"
                    "不要添加解释、不要使用契约子集之外的构造（如原始 HTML 标签）、"
                    "不要改动数学与代码定界符内部内容。"
                ),
            ),
            Message(
                role="user",
                content=(
                    f"[工具错误]\n{tool_error}\n\n"
                    f"[章节 {section.section_id} 当前 Normalized Markdown]\n{section_md}"
                ),
            ),
        ]
        resp = self._llm.complete(messages)
        content = getattr(resp, "content", None)
        if content is None:
            return None
        return str(content)

    # ------------------------------------------------------------------ #
    # 内部：契约校验（Req 10.6）
    # ------------------------------------------------------------------ #

    @staticmethod
    def _is_acceptable(corrected: str | None, ws: PaperWorkspace) -> bool:
        """判定 LLM 输出是否可采纳：非空可解析且不含契约外构造。

        经 ``Content_Contract.normalize`` + ``validate`` 校验；出现任一
        ``unknown_construct`` 违规即丢弃（Req 10.6 / Property 23）。其余诊断类
        违规（未知引用 / 图表 / 超长）不在本关卡阻断——保持关卡聚焦于“可解析 +
        无契约外构造”，与设计一致。
        """

        if corrected is None:
            return False
        text = str(corrected)
        if not text.strip():
            # 空 / 空白输出视为无法解析。
            return False
        normalized = content_contract.normalize(text)
        violations = content_contract.validate(normalized.content, ws)
        for v in violations:
            if getattr(v, "kind", "") == "unknown_construct":
                return False
        return True

    # ------------------------------------------------------------------ #
    # 内部：报告解析辅助
    # ------------------------------------------------------------------ #

    @staticmethod
    def _extract_tool_error(report: FormatGateReport) -> str:
        """从报告汇聚一段 ≤2000 字符的工具错误文本（Req 10.1，脱敏）。"""

        parts: list[str] = []
        for t in getattr(report, "tool_results", None) or []:
            name = getattr(t, "tool_name", "") or "tool"
            code = getattr(t, "exit_code", None)
            stderr = getattr(t, "stderr_excerpt", "") or ""
            if getattr(t, "timed_out", False):
                parts.append(f"[{name}] 超时终止：{stderr}")
            elif getattr(t, "missing", False):
                parts.append(f"[{name}] 工具缺失/不可执行：{stderr}")
            elif code not in (0, None):
                parts.append(f"[{name}] 退出码 {code}：{stderr}")
        for frag in getattr(report, "offending_fragments", None) or []:
            loc = getattr(frag, "location", "") or ""
            excerpt = getattr(frag, "excerpt", "") or ""
            parts.append(f"[出错定位 {loc}] {excerpt}")
        return _truncate("\n".join(p for p in parts if p), _TOOL_ERROR_MAX)

    @staticmethod
    def _error_category(report: FormatGateReport) -> str:
        """归纳工具错误类别，供尝试记录（Req 10.8，不含敏感内容）。"""

        results = list(getattr(report, "tool_results", None) or [])
        if any(getattr(t, "timed_out", False) for t in results):
            return "timeout"
        if getattr(report, "missing_tools", None):
            return "missing_tool"
        if any(getattr(t, "missing", False) for t in results):
            return "missing_tool"
        for t in results:
            code = getattr(t, "exit_code", None)
            if code not in (0, None):
                name = getattr(t, "tool_name", "") or "tool"
                return f"{name}_exit_{code}"
        return "unknown"

    @staticmethod
    def _status_from_report(report: FormatGateReport | None) -> RepairTerminalStatus:
        """依据（最近一次）报告判定终止状态：通过→已修复，否则→已耗尽。"""

        if report is not None and bool(getattr(report, "passed", False)):
            return RepairTerminalStatus.REPAIRED_WITHIN_LIMIT
        return RepairTerminalStatus.REPAIR_EXHAUSTED

    # ------------------------------------------------------------------ #
    # 内部：定位出错章节
    # ------------------------------------------------------------------ #

    @staticmethod
    def _offending_section(
        ws: PaperWorkspace, report: FormatGateReport
    ) -> SectionDraft | None:
        """确定要修复的章节：优先用出错片段的 ``section_id``，否则取第一章。

        防御式：出错片段可能未带 ``section_id``（格式闸难以从工具 stderr 反推），
        此时回退到按大纲顺序的首个有草稿的章节；再退化到任一章节草稿。
        """

        drafts = getattr(ws, "section_drafts", None) or {}
        # 1) 出错片段带 section_id 且命中草稿。
        for frag in getattr(report, "offending_fragments", None) or []:
            sid = getattr(frag, "section_id", None)
            if sid and sid in drafts:
                return drafts[sid]
        # 2) 按大纲顺序取首个有草稿的章节。
        for node in ws.ordered_sections():
            if node.section_id in drafts:
                return drafts[node.section_id]
        # 3) 退化：任一草稿。
        if drafts:
            return next(iter(drafts.values()))
        return None


__all__ = ["FormatRepairLoop"]
