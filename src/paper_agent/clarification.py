"""确定性澄清：检测初稿结构缺口，就"要不要补章节 / 修订范围"征询用户。

动机：草稿修订时，用户给的初稿可能只有方法+实验。系统若擅自只润语言、或擅自
补引言，都可能不是用户想要的。本模块把这些**范围/结构决策**做成确定性的澄清问题，
经注入的 ``Elicitor`` 询问用户，产出一个可记录、可复现的 ``RevisionScope``。

设计：
- 触发是**确定性**的——用 ``section_types.infer_section_type`` 判断初稿缺哪些
  常规章节（引言/相关工作/结论），只在确有缺口时才问。
- 决策对象化——所有答案汇聚成 ``RevisionScope`` 并写入 ``ws.profile['revision_scope']``，
  下游据此行事、续跑不重复问、整轮可复现。
- 非交互（``AutoElicitor``）下所有问题取默认值 → 行为确定、向后兼容。

本模块纯逻辑、不调用 LLM、不写工作区（只产出决策对象与建议的新章节列表）。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from paper_agent.draft_analyzer import DraftGaps
from paper_agent.elicitation import Elicitor, Question
from paper_agent.parsing import StructuredParser
from paper_agent.prompts import templates
from paper_agent.prompts.section_types import SectionType, infer_section_type
from paper_agent.workspace.models import ParseStatus, PaperWorkspace

# 视为"常规必备"的章节体裁——缺失时值得询问用户是否补齐。
_CANONICAL: list[tuple[SectionType, str]] = [
    (SectionType.INTRODUCTION, "引言"),
    (SectionType.RELATED_WORK, "相关工作"),
    (SectionType.CONCLUSION, "结论"),
]

# 范围选项（供 CLI 展示 / 脚本化应答）。
SCOPE_LANGUAGE = "仅语言润色"
SCOPE_STRUCTURE = "语言润色 + 补全缺失章节"
SCOPE_FULL = "语言润色 + 补全章节 + 补充文献"
_SCOPE_OPTIONS = [SCOPE_LANGUAGE, SCOPE_STRUCTURE, SCOPE_FULL]


@dataclass
class RevisionScope:
    """一次修订的范围决策（可序列化为 dict 写入 ws.profile）。

    Attributes:
        polish_language: 是否做语言润色（默认恒真——修订至少包含润色）。
        add_missing_sections: 是否补全缺失的常规章节。
        add_citations: 是否补充并核验文献。
        sections_to_add: 若补章节，具体补哪些体裁（section_type 值列表）。
    """

    polish_language: bool = True
    add_missing_sections: bool = False
    add_citations: bool = False
    sections_to_add: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "polish_language": self.polish_language,
            "add_missing_sections": self.add_missing_sections,
            "add_citations": self.add_citations,
            "sections_to_add": list(self.sections_to_add),
        }

    @classmethod
    def from_dict(cls, data: dict) -> "RevisionScope":
        return cls(
            polish_language=bool(data.get("polish_language", True)),
            add_missing_sections=bool(data.get("add_missing_sections", False)),
            add_citations=bool(data.get("add_citations", False)),
            sections_to_add=[str(s) for s in (data.get("sections_to_add") or [])],
        )


def present_section_types(titles_and_ids: list[tuple[str, str]]) -> set[SectionType]:
    """据 (section_id, title) 列表推断初稿已含哪些章节体裁。"""
    present: set[SectionType] = set()
    for section_id, title in titles_and_ids:
        present.add(infer_section_type(section_id, title))
    return present


def missing_canonical(
    titles_and_ids: list[tuple[str, str]],
) -> list[tuple[SectionType, str]]:
    """返回初稿缺失的常规章节 ``[(SectionType, 中文名), ...]``（保持固定顺序）。"""
    present = present_section_types(titles_and_ids)
    return [(t, name) for (t, name) in _CANONICAL if t not in present]


def clarify_revision_scope(
    elicitor: Elicitor,
    titles_and_ids: list[tuple[str, str]],
) -> RevisionScope:
    """就修订范围与结构缺口征询用户，产出 ``RevisionScope``。

    - 先问总体范围（仅语言 / +补章节 / +补文献）；默认"仅语言润色"（最保守，
      非交互下即此项）。
    - 若选择了补章节且确有缺失的常规章节，再逐类确认要补哪些。

    纯决策：不修改任何输入，不写工作区。
    """
    scope_answer = elicitor.ask(
        Question(
            id="revision_scope",
            prompt="本次修订的范围是？",
            options=_SCOPE_OPTIONS,
            default=SCOPE_LANGUAGE,
        )
    )
    scope = RevisionScope(
        polish_language=True,
        add_missing_sections=scope_answer in (SCOPE_STRUCTURE, SCOPE_FULL),
        add_citations=scope_answer == SCOPE_FULL,
    )

    if scope.add_missing_sections:
        for section_type, name in missing_canonical(titles_and_ids):
            ans = elicitor.ask(
                Question(
                    id=f"add_section_{section_type.value}",
                    prompt=f"初稿缺少「{name}」章节，是否新增？",
                    options=["是", "否"],
                    default="否",
                )
            )
            if ans == "是":
                scope.sections_to_add.append(section_type.value)
        # 用户选了补章节但没有任何缺失章节可补 / 全部选否 → 回落为无结构改动。
        if not scope.sections_to_add:
            scope.add_missing_sections = False

    return scope


class ClarificationProposer:
    """动态澄清问题提出器（路径 B 受约束版）：让 LLM 据场景提出至多 N 条问题。

    与"固定问题"互补：固定问题覆盖高价值岔路口（范围/结构缺口），本提出器让模型
    针对具体论文提出我们没预置的澄清点。为避免问题疲劳与不可控，做了三重约束：
    - **数量上限** ``max_questions``（截断）；
    - **仅结构化解析成功（PARSED）才采用**——Mock/失败返回空列表，不问凑数问题；
    - 只在**澄清阶段调用一次**（非连续对话）。

    经注入的 ``StructuredParser`` 请求 JSON；纯产出 ``list[Question]``，不写工作区、
    不直接向用户提问（提问由编排器经 ``Elicitor`` 统一进行）。
    """

    def __init__(self, parser: StructuredParser, *, max_questions: int = 3) -> None:
        self._parser = parser
        self._max = max(0, int(max_questions))

    def propose(self, ws: PaperWorkspace) -> list[Question]:
        if self._max == 0:
            return []
        outline_titles = [n.title for n in ws.ordered_sections()]
        draft_excerpt = (ws.original_draft or "")[:800]
        outcome = self._parser.request_json(
            templates.propose_clarifying_questions(
                topic_background=ws.topic_background or "",
                input_mode=ws.input_mode.value,
                outline_titles=outline_titles,
                draft_excerpt=draft_excerpt,
                max_questions=self._max,
            ),
            required_keys=("questions",),
        )
        if outcome.status is not ParseStatus.PARSED or outcome.data is None:
            return []
        raw = outcome.data.get("questions")
        if not isinstance(raw, list):
            return []

        questions: list[Question] = []
        for i, item in enumerate(raw[: self._max]):
            if not isinstance(item, dict):
                continue
            prompt = str(item.get("prompt") or "").strip()
            if not prompt:
                continue
            qid = str(item.get("id") or f"llm_clarify_{i}").strip() or f"llm_clarify_{i}"
            options_raw = item.get("options")
            options = (
                [str(o) for o in options_raw if str(o).strip()]
                if isinstance(options_raw, list)
                else []
            )
            questions.append(
                Question(
                    id=qid,
                    prompt=prompt,
                    options=options,
                    default=str(item.get("default", "")),
                )
            )
        return questions


@dataclass
class ClarificationBatch:
    """一次性收集的澄清问题及其答案 → 落地后的决策。

    用 ``collect_clarification_questions(gaps)`` 据缺口构造一批 ``Question``，
    经 ``Elicitor.ask_batch`` 一屏问完，再用 ``apply_answers`` 把答案汇成
    ``RevisionScope`` 与若干澄清偏好（写入 ``ws.profile``）。

    Attributes:
        questions: 待问的问题列表（顺序即展示顺序）。
        scope_qid: 修订范围问题的 id（答案映射到 RevisionScope）。
        section_qids: 「是否补章节」问题的 id 列表（按 gaps.missing_sections 顺序）。
    """

    questions: list[Question] = field(default_factory=list)
    scope_qid: str = "revision_scope"
    section_qids: list[str] = field(default_factory=list)


def collect_clarification_questions(
    gaps: DraftGaps, *, include_scope: bool = True
) -> ClarificationBatch:
    """据 ``DraftGaps`` 收集一批澄清 ``Question``（一屏问完）。

    - **范围问题**（仅 ``include_scope`` 时）：三选一，默认「仅语言润色」。
    - **缺章节问题**：每个缺失的常规章节一条是/否问题，默认「否」。
    - **缺引用列表**：是否让系统补参考文献段，默认「保留现状」。
    - **数字声明无 artifact**：是否信任正文数字 / 用户上传数据核验，默认「信任」。
    - **输出格式冲突**：是否改回与输入一致的格式，默认「保持当前输出」。

    非交互下所有 default 即最终答案，行为确定、向后兼容。
    """
    batch = ClarificationBatch(questions=[])

    # 1) 范围问题。
    if include_scope:
        batch.questions.append(
            Question(
                id="revision_scope",
                prompt="本次修订的范围是？",
                options=_SCOPE_OPTIONS,
                default=SCOPE_LANGUAGE,
            )
        )

    # 2) 缺章节问题（每章一条）。
    for section_value, name in gaps.missing_sections:
        qid = f"add_section_{section_value}"
        batch.section_qids.append(qid)
        batch.questions.append(
            Question(
                id=qid,
                prompt=f"初稿缺少「{name}」章节，是否新增？",
                options=["是", "否"],
                default="否",
            )
        )

    # 3) 正文有 [id] 但无参考文献段。
    if gaps.missing_reference_list:
        batch.questions.append(
            Question(
                id="missing_refs",
                prompt="正文有 [id] 引用标注，但找不到参考文献段。如何处理？",
                options=[
                    "保留现状（你后续手工补）",
                    "系统据 [id] 检索并补全参考文献段",
                ],
                default="保留现状（你后续手工补）",
            )
        )

    # 4) 数字声明无 artifact。
    if gaps.numeric_claims_without_artifact:
        batch.questions.append(
            Question(
                id="numeric_claims",
                prompt="正文含数字声明（如 F1=0.87 / +3.2%），但未提供实验数据。如何处理？",
                options=[
                    "信任正文数字（不核验）",
                    "我会后续上传数据让系统核验",
                ],
                default="信任正文数字（不核验）",
            )
        )

    # 5) 输出格式冲突。
    if gaps.output_format_mismatch:
        batch.questions.append(
            Question(
                id="output_format",
                prompt=f"输出格式冲突：{gaps.output_format_hint} 是否改回与输入一致的格式？",
                options=["改回与输入一致", "保持当前输出格式"],
                default="改回与输入一致",
            )
        )

    return batch


def apply_clarification_answers(
    batch: ClarificationBatch, answers: dict[str, str]
) -> tuple[RevisionScope, dict[str, str]]:
    """把 ``ask_batch`` 的答案汇成 ``RevisionScope`` + 澄清偏好（写入 profile）。

    返回 ``(scope, preferences)``：
    - ``scope``：据范围问题与补章节问题构造（兼容旧 ``RevisionScope`` 消费方）。
    - ``preferences``：缺引用 / 数字 / 输出格式 三个澄清偏好，供下游 agent / 导出器
      作为「用户澄清偏好」参考（不强制改变行为，仅记录用户选择）。
    """
    scope_answer = answers.get(batch.scope_qid, SCOPE_LANGUAGE)
    scope = RevisionScope(
        polish_language=True,
        add_missing_sections=scope_answer in (SCOPE_STRUCTURE, SCOPE_FULL),
        add_citations=scope_answer == SCOPE_FULL,
    )

    # 据补章节问题的答案填充 sections_to_add。
    for qid in batch.section_qids:
        if answers.get(qid) == "是":
            # qid 形如 "add_section_introduction" → 取 section_type 值。
            section_value = qid.removeprefix("add_section_")
            scope.sections_to_add.append(section_value)
    if scope.sections_to_add:
        scope.add_missing_sections = True
    elif scope.add_missing_sections and not scope.sections_to_add:
        # 用户选了「补章节」范围但每章都选否 → 回落为无结构改动（与旧逻辑一致）。
        scope.add_missing_sections = False

    preferences: dict[str, str] = {}
    if "missing_refs" in answers:
        preferences["missing_refs"] = answers["missing_refs"]
    if "numeric_claims" in answers:
        preferences["numeric_claims"] = answers["numeric_claims"]
    if "output_format" in answers:
        preferences["output_format"] = answers["output_format"]

    return scope, preferences


def clarify_revision_scope_batch(
    elicitor: Elicitor, gaps: DraftGaps
) -> tuple[RevisionScope, dict[str, str]]:
    """据缺口一次性问完所有澄清问题（``ask_batch`` 一屏问完）。

    旧入口 ``clarify_revision_scope`` 的批量版：替代「先问范围、再逐章问」的
    一步一停体验。无缺口时直接返回默认 ``RevisionScope``（仅语言润色）+ 空偏好，
    不向用户提问——非交互下零影响。
    """
    batch = collect_clarification_questions(gaps, include_scope=True)
    if not batch.questions:
        return RevisionScope(), {}
    answers = elicitor.ask_batch(batch.questions)
    return apply_clarification_answers(batch, answers)


def build_inplace_reroute_question(gaps: DraftGaps) -> Question | None:
    """据缺口构造「继续原地润色 还是 改走完整管线」的单问题。

    in-place 路径（LaTeX/DOCX 原地润色）只能改语言，无法补章节/补引用/核验数字。
    若检测到这些缺口，问用户是否 reroute 到完整管线（``--rebuild`` 语义）。

    - ``output_format_mismatch`` 对 in-place 无意义（in-place 输出=输入，恒一致），
      故不纳入 reroute 触发条件。
    - 无 actionable 缺口时返回 ``None``（不向用户提问，非交互零影响）。

    返回的单问题 default 取「继续原地润色」（最保守——不强制改路径）。
    """
    actionable = DraftGaps(
        missing_sections=list(gaps.missing_sections),
        missing_reference_list=gaps.missing_reference_list,
        numeric_claims_without_artifact=gaps.numeric_claims_without_artifact,
    )
    if not actionable.any_gap():
        return None

    reasons: list[str] = []
    if actionable.missing_sections:
        names = "、".join(name for _v, name in actionable.missing_sections)
        reasons.append(f"缺常规章节（{names}）")
    if actionable.missing_reference_list:
        reasons.append("正文有引用标注但无参考文献段")
    if actionable.numeric_claims_without_artifact:
        reasons.append("含实验数字但未提供实验数据")
    prompt = (
        f"初稿检测到：{'；'.join(reasons)}。原地润色只改语言，"
        f"无法补章节/引用/核验数字。如何处理？"
    )
    return Question(
        id="inplace_vs_rebuild",
        prompt=prompt,
        options=[
            "继续原地润色（接受缺口，只改语言）",
            "改走完整管线（能补章节/引用，但会丢原排版）",
        ],
        default="继续原地润色（接受缺口，只改语言）",
    )


__all__ = [
    "present_section_types",
    "missing_canonical",
    "clarify_revision_scope",
    "ClarificationProposer",
    "ClarificationBatch",
    "collect_clarification_questions",
    "apply_clarification_answers",
    "clarify_revision_scope_batch",
    "build_inplace_reroute_question",
    "SCOPE_LANGUAGE",
    "SCOPE_STRUCTURE",
    "SCOPE_FULL",
]
