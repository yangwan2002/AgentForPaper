"""结构化 Prompt 模板（稳定优先，利于前缀缓存）。

设计原则（prompt caching 友好）：
每次调用的消息按"稳定 → 易变"排列：
  1. system：全局稳定（角色 + 规范），同类调用逐字节一致；
  2. run-context（user）：一次运行内稳定（大纲 + 术语表 + 已验证文献清单）；
  3. task（user）：本次易变（章节标题、已写摘要、修订建议等）。
前两段构成可被服务端前缀缓存命中的稳定前缀，降低 token 成本、加快首字。

所有函数统一返回 list[Message]，调用方直接交给 LLMProvider.complete。
"""

from __future__ import annotations

import json

from paper_agent.providers.llm.base import Message

# --- 稳定的 system 提示（含规范，放最前以利缓存） ---

WRITING_SYSTEM = (
    "你是一名严谨的学术论文写作助手。写作规范：\n"
    "- 逻辑清晰、论证充分、术语一致、语言专业；\n"
    "- 与全局大纲和已写章节衔接，避免重复与矛盾；\n"
    "- 只能引用提供的『已验证文献』，严禁编造引用或文献；\n"
    "- 需要引用时使用所提供文献清单中的条目标识。"
)

PLAN_SYSTEM = (
    "你是一名学术论文规划专家。你需要基于主题背景规划论文的章节大纲，"
    "并判断哪些章节需要检索外部文献支撑。仅输出要求的 JSON，不要附加说明。"
)

REVIEW_SYSTEM = (
    "你是一名严格的学术论文评审专家。你需要按给定维度客观评分（0-10），"
    "给出可执行、可定位到具体章节的修订建议。仅输出要求的 JSON，不要附加说明。"
)

PARSE_SYSTEM = (
    "你是一名文献信息抽取助手。把非结构化的参考文献条目解析为结构化字段，"
    "严格按要求输出 JSON，不要臆造不存在的信息。"
)

SEARCH_SYSTEM = (
    "你是一名学术检索助手。你需要把研究主题转化为精准的英文检索词，"
    "并判断候选文献与主题的相关性。仅输出要求的 JSON，不要附加说明。"
)


def expand_queries(*, topic: str, max_queries: int = 4) -> list[Message]:
    """把（可能是中文的）主题转成若干精准英文检索词。"""
    user = (
        f"将以下论文研究主题转化为 {max_queries} 个精准的**英文**学术检索关键词短语，"
        f"用于在英文学术库（OpenAlex/arXiv）检索强相关文献。\n"
        f"主题：{topic}\n"
        '请输出 JSON：{"queries": ["keyword phrase 1", "keyword phrase 2"]}。'
        "关键词应聚焦核心技术概念，避免整句。只输出 JSON。"
    )
    return [Message("system", SEARCH_SYSTEM), Message("user", user)]


def filter_relevant(*, topic: str, candidates: list[tuple[str, str]]) -> list[Message]:
    """从候选文献中筛出与主题真正相关的（按 id）。"""
    listing = "\n".join(f"- {cid} :: {title}" for cid, title in candidates)
    user = (
        f"下面是检索到的候选文献。请判断哪些与研究主题**确实相关**"
        f"（主题领域一致、可作为该论文的参考），剔除明显无关的。\n"
        f"研究主题：{topic}\n\n候选文献：\n{listing}\n\n"
        '请输出 JSON：{"relevant_ids": ["id1", "id2"]}，只列出相关文献的 id。'
        "若都不相关则返回空数组。只输出 JSON。"
    )
    return [Message("system", SEARCH_SYSTEM), Message("user", user)]


# --- 写作（稳定优先三段式） ---

def writing_section(
    *,
    title: str,
    hint: str,
    run_context: str,
    summaries: str,
    is_revision_base: bool = False,
    draft_excerpt: str = "",
    section_guidance: str = "",
) -> list[Message]:
    """初次撰写某章节。run_context 为稳定段，task 为易变段。

    Round 5：``section_guidance`` 携带按章节体裁差异化的写作规约（来自
    ``section_types.SectionTypeSpec.writing_guidance``）。提供时插在「本次任务」
    之后，把 Introduction / Method / Related Work 等的强约束送入 prompt。
    """
    task = [f"【本次任务】撰写论文章节《{title}》。"]
    if section_guidance:
        task.append(f"【章节体裁规约】\n{section_guidance}")
    if hint:
        task.append(f"本章应涵盖：{hint}")
    if summaries:
        task.append(f"已完成章节摘要（供衔接参考）：{summaries}")
    if is_revision_base and draft_excerpt:
        task.append(f"用户初稿参考片段：{draft_excerpt}")
    return [
        Message("system", WRITING_SYSTEM),
        Message("user", run_context),
        Message("user", "\n".join(task)),
    ]


def revise_section(
    *,
    title: str,
    suggestion: str,
    content: str,
    run_context: str,
    section_guidance: str = "",
) -> list[Message]:
    """基于评审建议局部修改某章节。

    Round 5：``section_guidance`` 提供章节体裁规约——修订时仍需遵守。
    """
    parts = [
        f"【局部修改】对章节《{title}》仅针对以下建议所指问题进行调整，"
        f"保留其余表述与结构不变，直接输出修改后的完整章节内容。"
    ]
    if section_guidance:
        parts.append(f"【章节体裁规约】\n{section_guidance}")
    parts.append(f"修订建议：{suggestion}\n章节原文：{content}")
    task = "\n".join(parts)
    return [
        Message("system", WRITING_SYSTEM),
        Message("user", run_context),
        Message("user", task),
    ]


def summarize_section(*, title: str, content: str) -> list[Message]:
    return [
        Message("system", WRITING_SYSTEM),
        Message(
            "user",
            f"用一到两句话概括论文章节《{title}》的核心内容，"
            f"用于后续写作的上下文参考：\n{content}",
        ),
    ]


def figure_caption(*, data_ref: str) -> list[Message]:
    return [
        Message("system", WRITING_SYSTEM),
        Message(
            "user",
            f"请为以下实验数据/图表生成一句简洁、准确的学术图表说明（caption）：\n{data_ref}",
        ),
    ]


# --- 规划 ---

def plan_outline(
    *,
    topic_background: str,
    input_mode: str,
    draft_excerpt: str = "",
    artifact_contract: str = "",
) -> list[Message]:
    user = (
        "请基于以下信息规划论文大纲。\n"
        f"[输入模式]{input_mode}\n"
        f"[主题背景]\n{topic_background or '（未提供，请根据初稿推断）'}\n"
    )
    if draft_excerpt:
        user += f"[已有初稿片段]\n{draft_excerpt}\n"
    if artifact_contract:
        user += (
            "[事实契约：只能规划有下列证据支撑的章节]\n"
            f"{artifact_contract}\n"
        )
    user += (
        "\n请输出 JSON："
        '{"sections": [{"section_id": "英文短标识", "title": "章节标题", '
        '"summary_hint": "本章应涵盖的要点", "needs_retrieval": true/false, '
        '"required_evidence_ids": ["必须使用的事实证据ID"], '
        '"allowed_evidence_ids": ["本章允许使用的事实证据ID"]}]}。'
        "证据ID必须逐字来自事实契约，不得创造方法、实验、数据集或基线。"
        "needs_retrieval 表示该章节是否需要检索外部文献支撑。只输出 JSON。"
    )
    return [Message("system", PLAN_SYSTEM), Message("user", user)]


# --- 评审 ---

def review_paper(
    *,
    paper_text: str,
    dimensions: list[str],
    section_rubrics: str = "",
    artifact_contract: str = "",
    deterministic_report: str = "",
) -> list[Message]:
    dim_desc = (
        "- logic（逻辑性）：论证链条是否清晰、连贯\n"
        "- novelty（新颖性）：观点/方法是否有新意\n"
        "- sufficiency（论证充分性）：证据、实验、引用是否充分\n"
        "- language（语言质量）：表达是否准确、专业、流畅"
    )
    dim_keys = "、".join(dimensions)
    rubric_block = (
        f"\n[按章节体裁的额外强制检查项（请在相应维度与 section_feedback 中据此评判）]\n"
        f"{section_rubrics}\n"
        if section_rubrics
        else ""
    )
    user = (
        f"请对以下论文按这些维度（{dim_keys}）各打 0-10 分（可含小数），并给出修订建议。\n"
        f"维度说明：\n{dim_desc}\n"
        f"{rubric_block}\n"
        + (
            f"[用户事实契约：正文不得偏离]\n{artifact_contract}\n"
            if artifact_contract
            else ""
        )
        + (
            f"[确定性偏差报告：high 项必须导致 sufficiency 不通过]\n"
            f"{deterministic_report}\n"
            if deterministic_report
            else ""
        )
        +
        f"[论文内容]\n{paper_text}\n\n"
        "请严格输出如下 JSON（键名必须用上面的英文维度键）：\n"
        '{"scores": {"logic": 0-10, "novelty": 0-10, "sufficiency": 0-10, '
        '"language": 0-10}, '
        '"suggestions": {"logic": "建议", "novelty": "建议", '
        '"sufficiency": "建议", "language": "建议"}, '
        '"section_feedback": {"<章节标题或section_id>": "该章节具体需改进之处"}}'
    )
    return [Message("system", REVIEW_SYSTEM), Message("user", user)]


# --- 对抗式评审（adversarial reviewer，打破自评 reward-hack） ---

ADVERSARIAL_SYSTEM = (
    "你是一名严苛的对抗式学术评审，默认对论文持 reject 立场——你的任务是"
    "找出论文中至少 3 条具体、可指认的弱点（weakness），而不是赞美。\n"
    "评判原则：\n"
    "- 默认 reject：除非你**找不到任何**实质性弱点，否则不应通过；\n"
    "- 具体性：每条 weakness 必须指向具体章节/句段/缺失证据，不接受空泛措辞"
    "  （如「论证可以更充分」「语言可以更流畅」）；\n"
    "- 学术硬伤优先：claim 缺证据、与现有工作差异表述虚假、实验缺关键对比、"
    "  方法可复现性缺失（数据集/超参/随机种子）、figure/table 与正文数字不一致、"
    "  **正文内容不在用户提供的 artifact 中（fabricated_content）**。\n"
    "仅输出要求的 JSON，不要附加说明。"
)


def adversarial_review_paper(
    *,
    paper_text: str,
    min_weaknesses: int = 3,
    artifact_context: str = "",
) -> list[Message]:
    """对抗式评审：默认 reject，必须列出 ≥ min_weaknesses 条 weakness。

    Round 7：``artifact_context`` 携带用户提供的真实研究内容（方法/贡献/实验数值）。
    存在时，对抗审应专门找出正文中「不在 artifact 里的内容」标为 fabricated_content。

    返回 JSON：
    - ``decision``: "reject" | "borderline" | "accept"——只有"找不到任何实质性
      弱点"时才能选 accept；只要存在 ≥1 条具体弱点至少应为 borderline。
    - ``weaknesses``: ``[{"section_id": ..., "category": ..., "issue": ...,
      "suggested_fix": ...}, ...]``——每条必须具体、可指认。
    - ``critical_count``: weaknesses 中标记为 critical 的条数。
    """
    parts = [
        f"请以默认 reject 的立场严格审阅以下论文，找出**至少 {min_weaknesses} 条**"
        "具体、可指认的弱点（weakness）。空泛措辞不计入。"
    ]
    if artifact_context:
        parts.append(
            f"【用户提供的真实研究内容（正文必须基于此；不得编造）】\n{artifact_context}"
        )
    parts.append(f"[论文内容]\n{paper_text}")
    parts.append(
        "请严格输出如下 JSON：\n"
        '{"decision": "reject" | "borderline" | "accept", '
        '"weaknesses": [{"section_id": "章节id或标题", '
        '"category": "claim_unsupported|missing_evidence|reproducibility'
        '|novelty_overclaim|inconsistency|fabricated_content|other", '
        '"severity": "critical|major|minor", '
        '"issue": "具体问题描述（指明位置/缺失什么；若为 fabricated_content，'
        "说明该内容在 artifact 中找不到）\", "
        '"suggested_fix": "具体可执行的修复建议"}], '
        '"critical_count": 0}\n'
        f"提醒：若实在找不到 {min_weaknesses} 条以上弱点，decision 才可为 accept；"
        "否则 decision 至少为 borderline。"
    )
    user = "\n\n".join(parts)
    return [Message("system", ADVERSARIAL_SYSTEM), Message("user", user)]


# --- 文献解析 ---

def parse_references(*, ref_section: str) -> list[Message]:
    user = (
        "请将以下参考文献列表逐条解析为结构化数据。\n\n"
        f"[参考文献文本]\n{ref_section}\n\n"
        "请严格输出 JSON：\n"
        '{"references": [{"index": 序号, "title": "标题", '
        '"authors": ["作者1","作者2"], "year": 年份数字或null, '
        '"doi": "DOI或空字符串"}]}\n'
        "缺失的字段留空或 null，不要编造。只输出 JSON。"
    )
    return [Message("system", PARSE_SYSTEM), Message("user", user)]


# --- 引用忠实性判定（citation faithfulness judge） ---

FAITHFULNESS_JUDGE_SYSTEM = (
    "你是严格的引用忠实性判定器。你只能依据【给定的 grounding 文本】判断被引文献"
    "是否支撑该声明句，严禁使用你自己的知识或记忆。\n"
    "判定规范：\n"
    "- 只看 grounding：所有结论必须能在提供的 grounding 文本中找到依据，不得引入"
    "  grounding 之外的任何信息；\n"
    "- grounding 不足以判断时，必须选择 cannot_verify，绝不臆测为 supported；\n"
    "- 仅输出要求的 JSON，不要附加任何说明、前后缀或解释。"
)


def judge_citation_faithfulness(
    *, claim: str, grounding: str, reference_meta: str
) -> list[Message]:
    """判定某声明句是否被其被引文献支撑。

    稳定段 = ``FAITHFULNESS_JUDGE_SYSTEM``（角色 + 判定规范）；
    易变段 = claim + grounding + reference_meta（仅这三者，不含其它章节正文
    或记忆提示，Req 3.1）。要求模型只依据 grounding 判定，grounding 不足必选
    ``cannot_verify``，并仅输出规定 JSON。
    """
    user = (
        "请判断下面的【声明句】是否被其【被引文献的 grounding 文本】所支撑。"
        "只能依据 grounding 判断，不得使用你自己的知识；grounding 不足以判断时"
        "必须选择 cannot_verify。\n\n"
        f"【被引文献元信息】\n{reference_meta}\n\n"
        f"【声明句（claim）】\n{claim}\n\n"
        f"【grounding 文本】\n{grounding}\n\n"
        "请严格输出如下 JSON（不要附加任何说明）：\n"
        '{"verdict": "supported|weak_support|unsupported|cannot_verify", '
        '"rationale": "简短理由", '
        '"supporting_snippet": "grounding 中的片段或空"}'
    )
    return [Message("system", FAITHFULNESS_JUDGE_SYSTEM), Message("user", user)]


def judge_citation_faithfulness_batch(items: list[dict]) -> list[Message]:
    """Judge 8–16 isolated claim/grounding records in one provider call."""
    payload = json.dumps(items, ensure_ascii=False)
    user = (
        "请逐项判断下列引用声明。每项相互独立，只能使用该项自己的 grounding，"
        "不得跨项共享依据。输出 results 必须与输入 id 一一对应。\n\n"
        f"【批量输入】\n{payload}\n\n"
        "请严格输出 JSON："
        '{"results":[{"id":"输入id",'
        '"verdict":"supported|weak_support|unsupported|cannot_verify",'
        '"rationale":"简短理由","supporting_snippet":"该项grounding片段或空"}]}'
    )
    return [Message("system", FAITHFULNESS_JUDGE_SYSTEM), Message("user", user)]


FAITHFULNESS_DEEP_REVIEW_SYSTEM = (
    "你是引用忠实性的严格复核员。此前的批量判定只能确认该声明可能获得弱支撑；"
    "你必须从零开始独立复核，不能采纳、推断或延续此前判定。\n"
    "硬约束：\n"
    "- 只能使用本次提供的 grounding 文本，严禁使用模型知识、记忆、其它条目或"
    "此前判定；\n"
    "- supported 仅适用于 grounding 明确、直接且完整支持声明全部实质性内容；\n"
    "- grounding 明确反驳声明时选 unsupported；证据不完整、含糊、仅间接相关或"
    "无法确定时一律选 cannot_verify；\n"
    "- 仅输出要求的 JSON，不要附加说明。"
)


def deep_review_citation_faithfulness(
    *, claim: str, grounding: str, reference_meta: str
) -> list[Message]:
    """Independently re-review one weak result using grounding only."""
    user = (
        "请独立严格复核下面的单条声明。不要参考任何先前结论；只依据该条 grounding。"
        "若 grounding 未直接完整支撑声明的全部实质内容，必须选择 cannot_verify。\n\n"
        f"【被引文献元信息】\n{reference_meta}\n\n"
        f"【声明句（claim）】\n{claim}\n\n"
        f"【grounding 文本】\n{grounding}\n\n"
        "请严格输出如下 JSON（不要附加任何说明）：\n"
        '{"verdict": "supported|unsupported|cannot_verify", '
        '"rationale": "简短理由", '
        '"supporting_snippet": "grounding 中的直接证据片段或空"}'
    )
    return [
        Message("system", FAITHFULNESS_DEEP_REVIEW_SYSTEM),
        Message("user", user),
    ]


# --- 语言润色 / 一致性校对（language polish & consistency） ---

POLISH_SYSTEM = (
    "你是一名资深学术论文语言编辑（copy editor），母语级中英双语。你的唯一任务是"
    "在**不改变任何事实、论断、数据与结构**的前提下，提升给定章节的语言质量：\n"
    "- 修正语法、拼写、标点、时态与语态；\n"
    "- 统一术语与专有名词（同一概念全篇用词一致）；\n"
    "- 消除中英文混排（除专有名词/公式/代码/文献 id 外，正文语言应与原文主语言一致）；\n"
    "- 改善句子衔接与可读性，去除口语化、冗余与空泛套话。\n"
    "严格禁止：\n"
    "- 增删或改动任何数字、实验结果、公式；\n"
    "- 增删或改写方括号文献引用标注（形如 [arxiv:1706.03762] 的 [id] 必须逐字保留）；\n"
    "- 引入原文没有的新事实、新引用、新章节；\n"
    "- 增删 Markdown 标题层级或改变章节结构。\n"
    "只输出润色后的章节正文本身，不要输出任何解释、前后缀或代码块围栏。"
)


def polish_section(
    *,
    title: str,
    content: str,
    glossary_terms: str = "",
    section_guidance: str = "",
) -> list[Message]:
    """对单个章节做语言润色与一致性校对（不改变事实/数据/引用/结构）。

    稳定段 = ``POLISH_SYSTEM``（编辑角色 + 硬约束）；易变段 = 章节标题 + 体裁规约
    + 术语表 + 章节原文。要求模型只做语言层面的改写，逐字保留数字与 ``[id]`` 引用。

    Round 8：``section_guidance`` 携带按章节体裁的语言/结构惯例（来自
    ``section_types.SectionTypeSpec.writing_guidance``）。因 ``POLISH_SYSTEM`` 已硬约束
    「不新增事实/数字/引用、不改结构」，此处仅让润色对齐体裁惯例（如摘要更凝练、
    方法更精确），越界改动会被下游确定性守卫拦截并丢弃。
    """
    parts = [f"请润色论文章节《{title}》。仅做语言与一致性层面的改写。"]
    if section_guidance:
        parts.append(
            "【本章体裁语言惯例（仅在不新增内容前提下对齐；不得据此添加新事实/数据/引用）】\n"
            + section_guidance
        )
    if glossary_terms:
        parts.append(f"【全篇统一术语（请对齐这些用词）】\n{glossary_terms}")
    parts.append(f"【章节原文】\n{content}")
    parts.append("请直接输出润色后的完整章节正文（保留所有数字与 [id] 引用标注）。")
    return [Message("system", POLISH_SYSTEM), Message("user", "\n\n".join(parts))]


# --- LaTeX 原地润色（in-place source polish：只改散文，保结构） ---

LATEX_INPLACE_SYSTEM = (
    "你是一名资深学术论文语言编辑（copy editor），母语级中英双语。下面给你的是"
    "从一篇 LaTeX 文档正文中抽取的**一个纯散文片段**（其中的公式、命令、图表、"
    "引用已被移除或不在此片段内）。你的唯一任务是提升该片段的语言质量：\n"
    "- 修正语法、拼写、标点、时态与语态；\n"
    "- 统一术语；消除中英文混排；改善衔接与可读性；去除口语化与冗余套话。\n"
    "**绝对禁止**（违反则该片段会被系统丢弃、退回原文）：\n"
    "- 新增或删除任何以反斜杠开头的 LaTeX 命令（如 \\emph、\\textbf、\\%）；\n"
    "- 改变花括号 {}、方括号 []、美元符号 $ 的数量；\n"
    "- 增删或改动任何数字、以及形如 [id] 或 \\cite{...} 的引用；\n"
    "- 改变原意、增删事实、加入新内容或解释。\n"
    "只输出润色后的片段本身，不要输出任何解释、前后缀或代码块围栏。"
)


def polish_latex_prose(*, fragment: str) -> list[Message]:
    """润色从 LaTeX 正文抽取的单个散文片段（保结构、保引用、保数字）。

    稳定段 = ``LATEX_INPLACE_SYSTEM``；易变段 = 待润色片段。要求模型只做语言层
    改写，逐字保留 LaTeX 命令、花括号/美元符号数量、数字与 ``[id]``/``\\cite`` 引用。
    """
    return [
        Message("system", LATEX_INPLACE_SYSTEM),
        Message("user", f"【待润色片段】\n{fragment}"),
    ]


# --- 动态澄清问题（LLM 据场景提出，受数量约束） ---

CLARIFY_SYSTEM = (
    "你是一名资深学术论文合作者。在开始撰写/修订前，你只在**真正拿不准、且答案会"
    "实质改变论文写法**时，才向作者提出澄清问题——不要问可以自行合理决定的琐事，"
    "不要为了凑数而问。每个问题应具体、可用一句话回答，尽量给出候选项。"
    "仅输出要求的 JSON，不要附加说明。"
)


def propose_clarifying_questions(
    *,
    topic_background: str,
    input_mode: str,
    outline_titles: list[str],
    draft_excerpt: str = "",
    max_questions: int = 3,
) -> list[Message]:
    """让模型据当前论文场景提出至多 ``max_questions`` 条澄清问题（可为 0 条）。

    输出 JSON：``{"questions": [{"id": "短标识", "prompt": "问题",
    "options": ["选项..."](可空), "default": "默认答案"(可空)}]}``。
    模型被要求"无实质不确定时返回空数组"，从而不问凑数问题。
    """
    titles = "、".join(outline_titles) if outline_titles else "（暂无大纲）"
    parts = [
        f"论文场景：输入模式={input_mode}；当前大纲章节：{titles}。",
        f"主题背景：{topic_background or '（未提供）'}",
    ]
    if draft_excerpt:
        parts.append(f"初稿片段：{draft_excerpt[:800]}")
    parts.append(
        f"请提出**至多 {max_questions} 条**真正影响论文写法的澄清问题；"
        "若没有实质不确定，返回空数组。严格输出 JSON：\n"
        '{"questions": [{"id": "scope_or_topic_short_id", "prompt": "一句话问题", '
        '"options": ["候选1", "候选2"], "default": "默认答案"}]}。'
        "options / default 可省略或留空。只输出 JSON。"
    )
    return [Message("system", CLARIFY_SYSTEM), Message("user", "\n".join(parts))]


# --- 纯散文润色（docx 原地：只改文字，保结构） ---

PLAIN_PROSE_SYSTEM = (
    "你是一名资深学术论文语言编辑（copy editor），母语级中英双语。下面给你的是"
    "从一篇 Word 文档正文中抽取的**一个段落的纯文本**。只润色其语言质量："
    "修正语法、拼写、标点、时态；统一术语；消除中英文混排；改善衔接与可读性、"
    "去除口语化与冗余套话。\n"
    "**绝对禁止**（违反则该段会被系统丢弃、退回原文）：\n"
    "- 增删或改动任何数字、以及形如 [id]/[1] 的引用标注；\n"
    "- 改变原意、增删事实、加入新内容或解释；\n"
    "- 输出任何标记、编号、标题符号或代码围栏。\n"
    "只输出润色后的该段纯文本本身。"
)


def polish_plain_prose(*, fragment: str) -> list[Message]:
    """润色一个纯文本段落（docx 原地润色用）：只改语言，保数字与引用标注。"""
    return [
        Message("system", PLAIN_PROSE_SYSTEM),
        Message("user", f"【待润色段落】\n{fragment}"),
    ]


# --- 术语抽取（主动构建术语表，供润色统一用词） ---

TERMINOLOGY_SYSTEM = (
    "你是一名学术论文术语规范助手。你的任务是从论文正文中抽取**核心术语/专有名词**，"
    "并为每个术语给出一个应在全篇统一使用的**规范写法**（canonical form）。\n"
    "规范：只抽取论文确实出现的、领域相关的关键术语（不臆造）；优先那些**全文出现过"
    "不一致写法**的术语（大小写/中英/缩写不一）；仅输出要求的 JSON，不要附加说明。"
)


def extract_terminology(*, paper_text: str, max_terms: int = 15) -> list[Message]:
    """从论文正文抽取至多 ``max_terms`` 个核心术语及其规范写法。

    输出 JSON：``{"terms": [{"term": "规范写法", "definition": "一句话说明或空"}]}``。
    ``term`` 即建议全篇统一采用的规范写法。
    """
    user = (
        f"请从下面的论文正文中抽取至多 {max_terms} 个核心术语，"
        "为每个给出应全篇统一使用的规范写法。\n\n"
        f"[论文正文]\n{paper_text}\n\n"
        '请严格输出 JSON：{"terms": [{"term": "规范写法", "definition": "一句话说明或空"}]}。'
        "只输出 JSON。"
    )
    return [Message("system", TERMINOLOGY_SYSTEM), Message("user", user)]
