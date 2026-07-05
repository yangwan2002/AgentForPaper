"""确定性质量闸（客观检查，不依赖 LLM 主观评分）。

借鉴编程 agent 用"测试/编译"作客观验证的思路：论文虽无 ground truth，
但仍有一批可机械检查的质量信号，用来补强 Review_Agent 的自评偏差。

检查项（高/中/低 严重度）：
- 缺失/空章节（high）：大纲中的章节没有草稿或内容为空。
- 残留占位（high）：正文含 TODO/待补充/XXX/[填写] 等未完成标记。
- 非法引用（high）：章节引用了不在已验证文献库中的 id（违反 Property 1）。
  含两条互补路径——记录的 ``cited_reference_ids`` 与正文里实际出现的 ``[id]``
  标注：任一引用了未核验 id 即判 high。正文扫描避免「局部修订后正文新冒出伪造
  引用、而记录字段未同步」的绕过（Property 1 修订路径修复）。
- 章节过短（medium）：内容长度低于阈值，疑似未展开。
- 全文零引用（medium）：存在已验证文献却没有任何章节引用。
- 体裁必备元素缺失（Round 5）：按推断的章节类型（``SectionType``）的
  ``required_elements`` 检查关键词集合是否在正文中出现；任一类别完全缺失即
  记 high severity——例如 Method 缺超参/数据集、Limitations 缺具体局限。

输出结构化报告；高严重度问题会阻止"质量达标"提前终止（但不阻止迭代上限终止）。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from paper_agent.prompts.section_types import infer_section_type, get_spec
from paper_agent.workspace.models import PaperWorkspace

_PLACEHOLDER = re.compile(
    r"(TODO|FIXME|待补充|待完善|此处填写|\[填写\]|XXX|\?\?\?|tbd)", re.IGNORECASE
)

# 正文里形如 [id] 的引用标注；id 限 ASCII 标识符字符（含冒号/点/连字符），
# 避免误捕获 [表格 第1页 #1] 这类含空格/CJK 的非引用方括号。
_TEXT_CITATION = re.compile(r"\[([A-Za-z0-9_.:\-]+)\]")


# LaTeX 交叉引用标签前缀：``\ref{eq:..}`` / ``\label{tab:..}`` 等经文本抽取后会残留成
# ``[eq:..]`` / ``[tab:..]``，是**公式/图表/章节**的交叉引用，不是文献引用编号。识别规则
# 为"形如 prefix:name 且 prefix ∈ 该集合"，故真实带冒号引用（如 ``[arxiv:1706]``）因
# arxiv 不在集内不受影响。全小写比对，大小写不敏感。
_LATEX_REF_PREFIXES = frozenset({
    "eq", "eqn", "equation", "tab", "table", "fig", "figure", "sec", "section",
    "subsec", "subsection", "alg", "algorithm", "thm", "theorem", "lem", "lemma",
    "def", "definition", "cor", "corollary", "prop", "proposition", "app",
    "appendix", "chap", "chapter", "lst", "listing", "line", "item", "rem",
    "remark", "assumption", "asm", "ex", "example", "part", "step", "fig",
})


def _is_doc_type_marker(cid: str) -> bool:
    """判断 ``[cid]`` 是否为 GB/T 7714 文献类型标识（``[J]`` 期刊 / ``[C]`` 会议 /
    ``[M]`` 专著 / ``[D]`` 学位论文 / ``[EB]`` 电子公告 等），而非真实引用编号。

    这类标记是中文著录格式的一部分（如"……[J]. 计算机学报, 2020."），特征是**短、纯
    大写字母**；真实引用编号要么含数字（``[1]`` / ``[Smith2020]`` / ``[arxiv:1706]``）、
    要么较长，故用"长度 ≤2 且全大写字母"精准识别、不误伤真实引用。
    """
    return len(cid) <= 2 and cid.isalpha() and cid.isupper()


def _is_latex_ref_label(cid: str) -> bool:
    """判断 ``[cid]`` 是否为 LaTeX 交叉引用标签（如 ``[eq:relay_chain]`` /
    ``[tab:mask_tier]`` / ``[fig:overview]``），而非文献引用编号。

    这类是 ``\\ref{eq:..}`` / ``\\eqref{..}`` / ``\\label{..}`` 经文本抽取后残留的方括号，
    识别规则：形如 ``prefix:name`` 且 ``prefix`` ∈ :data:`_LATEX_REF_PREFIXES`（大小写
    不敏感）。真实带冒号引用（如 ``[arxiv:1706]``）因前缀不在集内不受影响。
    """
    if ":" not in cid:
        return False
    prefix = cid.split(":", 1)[0].strip().lower()
    return prefix in _LATEX_REF_PREFIXES


def _is_non_citation_marker(cid: str) -> bool:
    """``[cid]`` 是否为**非文献引用**的方括号标记（著录类型标识或 LaTeX 交叉引用标签）。

    引用扫描（质量闸/护栏审计/忠实性核验）统一复用本规则，保证"什么算引用"三处一致。
    """
    return _is_doc_type_marker(cid) or _is_latex_ref_label(cid)


def extract_text_citations(content: str) -> list[str]:
    """抽取正文里所有 ``[id]`` 形式的引用标注 id（保持出现顺序，去重）。

    排除**非文献引用**的方括号：GB/T 7714 著录类型标识（``[J]`` / ``[C]`` / ``[M]`` 等）
    与 LaTeX 交叉引用标签（``[eq:..]`` / ``[tab:..]`` / ``[fig:..]`` 等）——它们不是引用
    编号，否则会被护栏/收尾验收误判为"未核验的悬空引用"。
    """
    seen: list[str] = []
    for m in _TEXT_CITATION.finditer(content or ""):
        cid = m.group(1)
        if _is_non_citation_marker(cid):
            continue
        if cid not in seen:
            seen.append(cid)
    return seen


def build_allowed_values(artifact) -> list[float]:
    """构造 artifact grounding 检查允许的数值集合（去重排序）。

    = ``artifact.all_numeric_values()`` ∪ 每指标 stats 的衍生集合
    ``{mean, mean-std, mean+std, min, max}``。逐字节复刻原有构造顺序与去重逻辑：
    以 all_numeric_values() 为基础 set，遍历各实验的 stats，累加 mean/mean±std
    与 min/max，最后 ``sorted`` 返回。
    """
    # 扩展 allowed：加入 mean ± std 范围（实验结果的常见表述）
    extended_allowed: set[float] = set(artifact.all_numeric_values())
    for exp in artifact.experiments:
        stats = (exp.results_data or {}).get("stats") or {}
        for metric, s in stats.items():
            if not isinstance(s, dict):
                continue
            mean = s.get("mean")
            std = s.get("std")
            if mean is not None and std is not None:
                # 加入 mean, mean±std, min, max
                extended_allowed.add(float(mean))
                extended_allowed.add(float(mean) - float(std))
                extended_allowed.add(float(mean) + float(std))
            for key in ("min", "max"):
                v = s.get(key)
                if v is not None:
                    extended_allowed.add(float(v))
    return sorted(extended_allowed)


def value_matches(extracted: float, allowed: list[float], tolerance: float = 0.01) -> bool:
    """检查 extracted 是否在 allowed 中（浮点容差）。

    对百分比/小数用相对容差（1%），对大整数用绝对容差（±0.01）。
    """
    for a in allowed:
        if abs(a) < 1e-9:
            # 零值
            if abs(extracted) < tolerance:
                return True
        else:
            # 相对容差 1%
            if abs(extracted - a) / max(abs(a), 1e-9) <= tolerance:
                return True
    return False


@dataclass
class QualityReport:
    issues: list[dict] = field(default_factory=list)

    @property
    def high_issues(self) -> list[dict]:
        return [i for i in self.issues if i.get("severity") == "high"]

    @property
    def passed(self) -> bool:
        """无高严重度问题即视为通过确定性闸。"""
        return not self.high_issues

    def section_ids(self) -> set[str]:
        return {i["section_id"] for i in self.issues if i.get("section_id")}


class QualityGate:
    def __init__(self, min_section_chars: int = 80) -> None:
        self._min_chars = min_section_chars

    def check(self, ws: PaperWorkspace) -> QualityReport:
        issues: list[dict] = []
        verified_ids = ws.verified_reference_ids()
        any_citation = False

        for node in ws.ordered_sections():
            draft = ws.section_drafts.get(node.section_id)
            if draft is None or not draft.content.strip():
                issues.append({
                    "type": "empty_section",
                    "severity": "high",
                    "section_id": node.section_id,
                    "message": f"章节《{node.title}》缺失或为空，需补全内容。",
                })
                continue
            content = draft.content
            if _PLACEHOLDER.search(content):
                issues.append({
                    "type": "placeholder",
                    "severity": "high",
                    "section_id": node.section_id,
                    "message": f"章节《{node.title}》含未完成占位（TODO/待补充等），需补全。",
                })
            if len(content.strip()) < self._min_chars:
                issues.append({
                    "type": "too_short",
                    "severity": "medium",
                    "section_id": node.section_id,
                    "message": f"章节《{node.title}》内容过短（<{self._min_chars} 字），疑似未展开。",
                })
            # 非法引用：记录的 cited_reference_ids 不在已验证库（违反 Property 1）。
            for rid in draft.cited_reference_ids:
                if rid not in verified_ids:
                    issues.append({
                        "type": "invalid_citation",
                        "severity": "high",
                        "section_id": node.section_id,
                        "message": f"章节《{node.title}》引用了未经核验的文献 id：{rid}。",
                    })
            # 正文实际出现的 [id] 标注：捕捉记录字段未同步的伪造引用（修订路径修复）。
            for rid in extract_text_citations(content):
                if rid not in verified_ids:
                    issues.append({
                        "type": "text_citation_invalid",
                        "severity": "high",
                        "section_id": node.section_id,
                        "message": f"章节《{node.title}》正文标注了未经核验的文献 id：{rid}。",
                    })
            if draft.cited_reference_ids or extract_text_citations(content):
                any_citation = True

            # Round 5：按章节体裁的必备元素检查（缺失关键元素 → high severity）。
            self._check_required_elements(node, draft.content, issues)

        # Round 7：artifact grounding 检查——正文数字必须能在 artifact 实验数据中找到。
        self._check_artifact_grounding(ws, issues)

        if verified_ids and not any_citation:
            issues.append({
                "type": "no_citation",
                "severity": "medium",
                "message": "已有可用的已验证文献，但全文未引用任何文献。",
            })

        return QualityReport(issues=issues)

    @staticmethod
    def _check_required_elements(node, content: str, issues: list[dict]) -> None:
        """按章节体裁的 ``required_elements`` 做关键词集合检查。

        每类必备元素是一组同义关键词；任一类别**完全没有**任何同义词在正文中
        出现即记 high severity——例如 Method 缺超参/数据集，Limitations 缺
        具体局限措辞。UNKNOWN 类型无必备元素 → 不检查（向后兼容）。

        匹配为子串、忽略大小写；不要求章节长度（empty/too_short 已另检查）。
        """
        section_type = infer_section_type(node.section_id, node.title)
        spec = get_spec(section_type)
        if not spec.required_elements:
            return
        content_lower = content.lower()
        for element_name, synonyms in spec.required_elements:
            if any(syn.lower() in content_lower for syn in synonyms):
                continue  # 任一同义词命中即通过
            issues.append({
                "type": "missing_required_element",
                "severity": "high",
                "section_id": node.section_id,
                "message": (
                    f"章节《{node.title}》缺失体裁必备元素「{element_name}」"
                    f"（{section_type.value} 章节应出现 {synonyms[0]} 等关键内容）。"
                ),
            })

    # --- Round 7: artifact grounding ---

    @staticmethod
    def _extract_numeric_values(text: str) -> list[float]:
        """抽取正文中所有数值（含小数、百分比），忽略序号/年份/超参等低信息量数字。

        匹配模式：
        - ``\b\d+\.\d+\b``：小数（如 83.4、0.001）
        - ``\b\d+\s*%``：百分比（如 92.3%）
        - ``\b\d{3,}\b``：3 位以上整数（避免 1、2、3 这类序号/超参）

        返回去重排序的 float 列表。
        """
        values: set[float] = set()
        # 小数
        for m in re.finditer(r"\b(\d+\.\d+)\b", text):
            try:
                values.add(float(m.group(1)))
            except ValueError:
                continue
        # 百分比
        for m in re.finditer(r"\b(\d+(?:\.\d+)?)\s*%", text):
            try:
                values.add(float(m.group(1)))
            except ValueError:
                continue
        # 3 位以上整数（排除年份 2020-2029，但保留如 100、1024 这类实验数值）
        for m in re.finditer(r"\b(\d{3,})\b", text):
            try:
                val = float(m.group(1))
                # 排除年份
                if not (2000 <= val <= 2030):
                    values.add(val)
            except ValueError:
                continue
        return sorted(values)

    @staticmethod
    def _value_matches(extracted: float, allowed: list[float], tolerance: float = 0.01) -> bool:
        """检查 extracted 是否在 allowed 中（浮点容差）。委托模块级 ``value_matches``。"""
        return value_matches(extracted, allowed, tolerance)

    def _check_artifact_grounding(self, ws: PaperWorkspace, issues: list[dict]) -> None:
        """Round 7：正文数字必须能在 artifact 实验数据中找到。

        仅当 ``ws.artifact`` 存在且非空时检查。对每个章节草稿抽取数字，
        若该数字不在 artifact 的 allowed values 中 → fabricated_metric (high)。

        设计要点：
        - 容差 1%（浮点比较）
        - 允许 artifact 数值 + 其衍生（如 mean ± std 范围内的值）
        - 向后兼容：无 artifact 时跳过检查
        """
        artifact = ws.artifact
        if artifact is None or artifact.is_empty():
            return

        allowed = artifact.all_numeric_values()
        if not allowed:
            # artifact 没有数值数据（只有方法/贡献文本），跳过
            return

        # 扩展 allowed：加入 mean ± std 范围（实验结果的常见表述）
        allowed_list = build_allowed_values(artifact)

        for node in ws.ordered_sections():
            draft = ws.section_drafts.get(node.section_id)
            if draft is None or not draft.content.strip():
                continue
            extracted = self._extract_numeric_values(draft.content)
            for val in extracted:
                if not self._value_matches(val, allowed_list):
                    issues.append({
                        "type": "fabricated_metric",
                        "severity": "high",
                        "section_id": node.section_id,
                        "message": (
                            f"章节《{node.title}》出现未在 artifact 中找到的数字「{val}」"
                            f"——正文数字必须来自用户提供的实验数据，不得编造。"
                        ),
                    })
