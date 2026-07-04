"""学术论文章节类型识别与差异化体裁约束（Round 5：section-typed prompts）。

此前 ``templates.writing_section`` 对 Introduction / Method / Related Work /
Experiments / Discussion **一视同仁**——只接 ``title + hint``，不区分章节体裁的
强约束。但学术论文不同章节的写作技能差异极大：

- ``Introduction`` 需要清晰回答 motivation / gap / contribution；
- ``Related Work`` 应有 taxonomy / contrast 而不是简单罗列；
- ``Method`` 必须可复现（数据/超参/随机种子/计算资源）；
- ``Experiments`` 必须有基线对比 + 显著性 + 消融；
- ``Discussion / Limitations`` 必须诚实指出本工作不足、不夸大。

本模块把这些**体裁规约**与**必备元素 checklist** 编码为数据驱动的
``SectionTypeSpec``，由 ``writing_agent`` 据 section title / id 推断类型并把对应
规约注入 prompt。质量闸亦可据 spec 的 ``required_elements`` 做客观检查
（缺失关键元素 → high severity）。

接入新 venue / 新体裁：在此处追加一条 ``SectionTypeSpec``，无需改 agent 代码。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class SectionType(str, Enum):
    """学术论文章节体裁类型。``UNKNOWN`` 用作未识别时的默认。"""

    ABSTRACT = "abstract"
    INTRODUCTION = "introduction"
    RELATED_WORK = "related_work"
    BACKGROUND = "background"
    METHOD = "method"
    EXPERIMENTS = "experiments"
    RESULTS = "results"
    DISCUSSION = "discussion"
    LIMITATIONS = "limitations"
    CONCLUSION = "conclusion"
    ETHICS = "ethics"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class SectionTypeSpec:
    """一种章节体裁的写作约束规约。

    Attributes:
        type: 章节类型。
        writing_guidance: 写作规约（差异化体裁约束），注入到 writing prompt。
        required_elements: 该类型章节**必备元素**的客观关键词集合（如 Method 必
            出现 "数据集" / "超参" / "随机种子" 等的同义形式）。供质量闸据正文
            做无 LLM 客观检查——任一类别完全缺失即记一条 high severity。
            形如 ``[("dataset", ["数据集", "dataset", "训练集"]), ...]``。
        review_rubric: 评审 / 对抗审针对此章节应额外关注的点（注入 review prompt
            的章节级反馈）。
    """

    type: SectionType
    writing_guidance: str
    required_elements: list[tuple[str, list[str]]] = field(default_factory=list)
    review_rubric: str = ""


# --- 体裁规约表（接新章节 = 加一条记录） ---

SPECS: dict[SectionType, SectionTypeSpec] = {
    SectionType.ABSTRACT: SectionTypeSpec(
        type=SectionType.ABSTRACT,
        writing_guidance=(
            "摘要应包含五要素，顺序与比例合理：(1) 背景与问题（1-2 句）；"
            "(2) 现有方法的局限（1 句）；(3) 本文方法的关键思想（1-2 句）；"
            "(4) 主要实验结果与对比基线的提升（含具体数值）；"
            "(5) 贡献与意义（1 句）。不引入未在正文出现的术语；不夸大；"
            "不出现 'we propose a novel ...' 这类空泛宣称。整段 150-250 词。"
        ),
        review_rubric=(
            "摘要是否覆盖五要素？是否出现具体数值结果？是否夸大贡献？"
        ),
    ),
    SectionType.INTRODUCTION: SectionTypeSpec(
        type=SectionType.INTRODUCTION,
        writing_guidance=(
            "引言遵循 motivation → gap → contribution 三段式：\n"
            "1. Motivation：领域为何重要、当前应用场景；\n"
            "2. Gap：现有工作明确的不足，**点名具体方法**而非'已有方法的局限'；\n"
            "3. Contribution：列出本文 3-5 条具体贡献（不是'我们做了 X'，"
            "   而是'我们首次实现/发现/证明 X，带来 Y 提升'）。\n"
            "末段给 paper roadmap（"
            "本文第 2 节回顾相关工作，第 3 节描述方法，...）。"
        ),
        required_elements=[
            ("contribution", ["贡献", "contribution", "我们提出", "we propose", "我们的贡献"]),
        ],
        review_rubric=(
            "引言是否清晰回答 motivation/gap/contribution 三问？gap 是否点名"
            "具体已有工作而非空泛表述？贡献是否具体可衡量？"
        ),
    ),
    SectionType.RELATED_WORK: SectionTypeSpec(
        type=SectionType.RELATED_WORK,
        writing_guidance=(
            "相关工作不是文献列表，而是**对比性叙事**：\n"
            "1. 按子主题 / taxonomy 分组（"
            "至少 2-3 个子主题），每组先讲共性思路，再点名代表性方法；\n"
            "2. 每个子主题结尾必须对比说明：「与本工作的关键差异在 X」"
            "  （不能只说'与我们不同'，必须指出具体维度）；\n"
            "3. 避免 'A et al. proposed B' 的流水账；用主动语态、强动词。"
        ),
        required_elements=[
            ("contrast", ["与本工作", "区别", "differ", "与之不同", "对比", "in contrast"]),
        ],
        review_rubric=(
            "相关工作是否有 taxonomy 分组？每组是否点明与本工作的具体差异？"
            "差异表述是否真实可验（避免 novelty overclaim）？"
        ),
    ),
    SectionType.BACKGROUND: SectionTypeSpec(
        type=SectionType.BACKGROUND,
        writing_guidance=(
            "背景章节只介绍**理解本文方法所必需**的预备知识——不要写成教科书。"
            "关键符号、公式在此处定义，后续章节复用相同记号。引用经典文献支撑"
            "非平凡的事实陈述。"
        ),
    ),
    SectionType.METHOD: SectionTypeSpec(
        type=SectionType.METHOD,
        writing_guidance=(
            "方法章节必须可复现。按以下顺序组织：\n"
            "1. Overview：一段概述（含一张 architecture 图引用）；\n"
            "2. 形式化定义：问题输入/输出、关键符号；\n"
            "3. 每个核心组件单独小节，含动机 + 公式 + 设计选择的理由；\n"
            "4. 算法伪代码（如适用）；\n"
            "5. 实现细节：数据预处理、损失函数、优化器、关键超参。\n"
            "避免'我们的方法很简单'这种弱化贡献的措辞。"
        ),
        required_elements=[
            ("formalization", ["定义", "formal", "符号", "notation", "given"]),
            ("hyperparameter", ["超参", "learning rate", "学习率", "batch size",
                                 "epoch", "optimizer", "优化器"]),
        ],
        review_rubric=(
            "方法是否可复现？关键超参/数据预处理/优化器是否齐备？"
            "符号是否前后一致？动机是否清晰？"
        ),
    ),
    SectionType.EXPERIMENTS: SectionTypeSpec(
        type=SectionType.EXPERIMENTS,
        writing_guidance=(
            "实验必须包含：\n"
            "1. 数据集描述（来源、规模、划分、预处理）；\n"
            "2. 基线方法（至少 3 个，含 SOTA），说明为何选这些；\n"
            "3. 评价指标定义；\n"
            "4. 实验设置：硬件、训练时间、随机种子、超参搜索范围；\n"
            "5. 主实验结果表（含均值±方差或显著性检验）；\n"
            "6. 消融实验（每个 design choice 至少一组对比）。\n"
            "避免只在单一数据集 / 单一指标上汇报。"
        ),
        required_elements=[
            ("dataset", ["数据集", "dataset", "训练集", "测试集", "validation"]),
            ("baseline", ["基线", "baseline", "对比方法", "SOTA", "对照"]),
            ("metric", ["指标", "metric", "准确率", "accuracy", "F1", "BLEU", "AUC"]),
        ],
        review_rubric=(
            "数据集/基线/指标是否齐备？是否汇报方差或显著性？是否有消融？"
            "硬件和随机种子是否说明？"
        ),
    ),
    SectionType.RESULTS: SectionTypeSpec(
        type=SectionType.RESULTS,
        writing_guidance=(
            "结果章节专注呈现实验数据：用表格汇报主指标（含 ±标准差或显著性），"
            "用图汇报趋势/对比。每个表/图前一句简介，后两句解读。"
            "不在此处推断原因（推断留给 Discussion）。"
        ),
    ),
    SectionType.DISCUSSION: SectionTypeSpec(
        type=SectionType.DISCUSSION,
        writing_guidance=(
            "讨论分三层：\n"
            "1. 结果解读：为什么本方法在 X 数据集上更好/在 Y 上更差？归因到具体"
            "  设计选择，避免'可能是因为...'的弱推测；\n"
            "2. 与相关工作的更深对比（不重复 Related Work 的陈述，重在解释"
            "  机制差异如何造成结果差异）；\n"
            "3. 影响：本工作对学界 / 业界的实际意义。"
        ),
    ),
    SectionType.LIMITATIONS: SectionTypeSpec(
        type=SectionType.LIMITATIONS,
        writing_guidance=(
            "Limitations 必须**诚实**指出 ≥3 条具体不足，每条形如「X 场景下方法"
            "失效，原因 Y，缓解方向 Z」。不接受'未来工作可以扩展'这类空泛措辞。"
            "诚实的 Limitations 是论文可信度的关键。"
        ),
        required_elements=[
            ("specific_limitation", ["局限", "limitation", "不足", "局限性", "失效", "无法处理"]),
        ],
        review_rubric=(
            "是否列出 ≥3 条具体 limitation？是否每条都点名场景与原因？"
            "是否承认了真正的弱点而非伪 limitation？"
        ),
    ),
    SectionType.CONCLUSION: SectionTypeSpec(
        type=SectionType.CONCLUSION,
        writing_guidance=(
            "结论不是摘要重复——它应：(1) 用 2-3 句重新表述本文贡献（与 Intro"
            "  呼应但措辞不同）；(2) 给出 2-3 条**可执行的**未来方向（不是"
            "  '我们将扩展该方法'，而是'下一步将在 X 数据上验证 Y 假设'）。"
        ),
    ),
    SectionType.ETHICS: SectionTypeSpec(
        type=SectionType.ETHICS,
        writing_guidance=(
            "Ethics / Broader Impact：讨论本工作的潜在社会影响，"
            "包括可能的滥用场景、对弱势群体的影响、数据隐私、可再现性。"
            "不接受 'no ethical concerns' 的搪塞式回答——必须给出过的论证。"
        ),
    ),
    SectionType.UNKNOWN: SectionTypeSpec(
        type=SectionType.UNKNOWN,
        writing_guidance=(
            "按通用学术写作规范：逻辑清晰、术语一致、论据充分、"
            "引用规范、与全局大纲衔接。"
        ),
    ),
}


# --- 章节类型推断 ---

# 关键词 → SectionType 的映射表（小写匹配 title / section_id）。
# 越具体的词放越前，避免 "introduction" 命中 "intro_method" 之类。
_KEYWORD_TO_TYPE: list[tuple[tuple[str, ...], SectionType]] = [
    (("abstract", "摘要"), SectionType.ABSTRACT),
    (("related_work", "related work", "相关工作", "related", "literature review", "文献综述"),
     SectionType.RELATED_WORK),
    (("background", "preliminar", "preliminaries", "预备知识", "背景"),
     SectionType.BACKGROUND),
    (("introduction", "intro", "引言", "绪论"), SectionType.INTRODUCTION),
    (("method", "methodology", "approach", "model", "方法", "技术", "我们的方法"),
     SectionType.METHOD),
    (("experiment", "experiments", "实验", "evaluation", "评估"),
     SectionType.EXPERIMENTS),
    (("result", "results", "结果"), SectionType.RESULTS),
    (("discussion", "讨论", "分析"), SectionType.DISCUSSION),
    (("limitation", "limitations", "局限"), SectionType.LIMITATIONS),
    (("conclusion", "conclude", "结论", "总结"), SectionType.CONCLUSION),
    (("ethics", "broader impact", "伦理", "社会影响"), SectionType.ETHICS),
]


def infer_section_type(section_id: str, title: str) -> SectionType:
    """按 ``section_id`` 与 ``title`` 推断章节体裁。

    匹配规则：``section_id``（精确小写）和 ``title``（小写子串）任一命中关键词
    即归入对应类型；按 ``_KEYWORD_TO_TYPE`` 顺序匹配（具体的优先）。无匹配
    返回 ``SectionType.UNKNOWN``。
    """
    sid_lower = (section_id or "").lower().strip()
    title_lower = (title or "").lower().strip()
    for keywords, section_type in _KEYWORD_TO_TYPE:
        for kw in keywords:
            if kw == sid_lower or kw in title_lower:
                return section_type
    return SectionType.UNKNOWN


def get_spec(section_type: SectionType) -> SectionTypeSpec:
    """按类型取规约；未注册时返回 UNKNOWN 的通用规约。"""
    return SPECS.get(section_type, SPECS[SectionType.UNKNOWN])


def infer_and_get_spec(section_id: str, title: str) -> SectionTypeSpec:
    """方便方法：一步推断 + 取规约。"""
    return get_spec(infer_section_type(section_id, title))


__all__ = [
    "SectionType",
    "SectionTypeSpec",
    "SPECS",
    "infer_section_type",
    "get_spec",
    "infer_and_get_spec",
]
