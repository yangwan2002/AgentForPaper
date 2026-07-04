"""从零生成模式的结构化访谈式录入（防 hallucination 的轻量源头）。

`ResearchArtifact` 的完整形态需要 YAML + 实验 CSV，门槛较高。但即便用户还没有
成体系的实验数据，只要能说清「在什么**领域**、解决什么**问题**、用了什么**方法**」，
就足以把从零生成从「全凭 LLM 编造」拉回到「基于用户真实研究方向」。

本模块提供：
- ``build_artifact_from_description(...)``：纯函数，把领域/问题/方法（及可选的贡献、
  新颖性）构造成一个最小 ``ResearchArtifact``（无实验 → grounding 数值检查自动跳过，
  但 ``is_empty()`` 为 False，因此不会被判为「LLM 推断版」）。
- ``run_intake(elicitor)``：交互式问答，经注入的 ``Elicitor`` 提问便于测试；用户可留空
  某些可选项。三个必填项（领域/问题/方法）任一为空则返回 ``None``（调用方据此
  决定是否降级为纯 topic 生成）。

设计与既有 loader 一致：纯数据、可序列化、不调用 LLM。
"""

from __future__ import annotations

from paper_agent.elicitation import AutoElicitor, Elicitor, Question
from paper_agent.workspace.research_artifact import (
    Contribution,
    MethodSpec,
    ResearchArtifact,
)


def build_artifact_from_description(
    *,
    field: str,
    problem: str,
    method: str,
    contributions: list[str] | None = None,
    novelty_claims: list[str] | None = None,
    notes: str = "",
) -> ResearchArtifact:
    """由领域/问题/方法（及可选项）构造最小 ``ResearchArtifact``。

    Args:
        field: 研究领域（如「空地协同 SLAM」）。
        problem: 所解决的问题（如「大视角与大尺度差图像匹配」）。
        method: 所用方法概述（自然语言一到数句）。
        contributions: 可选，若干条贡献声明；为空时不填 contributions。
        novelty_claims: 可选，关键新颖性声明。
        notes: 可选补充说明。

    Returns:
        ``ResearchArtifact``：``research_question`` 综合领域+问题，``method.overview``
        取 method；无 experiments（数值 grounding 自动跳过）。

    Raises:
        ValueError: 领域/问题/方法任一为空（去空白后）。
    """
    field = (field or "").strip()
    problem = (problem or "").strip()
    method = (method or "").strip()
    if not field or not problem or not method:
        raise ValueError("领域、问题、方法均为必填，且不能为空。")

    research_question = f"在{field}领域，解决{problem}"
    contribs = [
        Contribution(summary=c.strip())
        for c in (contributions or [])
        if c and c.strip()
    ]
    return ResearchArtifact(
        research_question=research_question,
        method=MethodSpec(overview=method),
        contributions=contribs,
        experiments=[],
        novelty_claims=[n.strip() for n in (novelty_claims or []) if n and n.strip()],
        notes=(notes or "").strip(),
    )


def _split_items(raw: str, max_items: int = 5) -> list[str]:
    """把「分号/中文分号/换行」分隔的自由文本拆成若干条目（去空、限量）。"""
    import re

    parts = [p.strip() for p in re.split(r"[；;\n]+", raw or "") if p.strip()]
    return parts[:max_items]


def run_intake(elicitor: Elicitor | None = None) -> ResearchArtifact | None:
    """交互式采集领域/问题/方法（+可选贡献、新颖性），构造 ``ResearchArtifact``。

    经注入的 ``Elicitor`` 提问（缺省 ``AutoElicitor``——非交互，返回各问题默认值）。
    三个必填项（领域/问题/方法）任一为空 → 返回 ``None``（调用方回退为纯 topic 生成
    并显式降级警告）。默认值为空字符串，故 ``AutoElicitor`` 下自然返回 ``None``，
    保持"非交互不追问"的语义。
    """
    elicitor = elicitor or AutoElicitor()

    field_ = elicitor.ask(Question("field", "1) 研究领域是什么？")).strip()
    problem = elicitor.ask(Question("problem", "2) 你的论文解决了该领域的什么问题？")).strip()
    method = elicitor.ask(
        Question("method", "3) 你使用的方法/技术路线是什么？（可多句）")
    ).strip()

    if not field_ or not problem or not method:
        return None

    contributions = _split_items(
        elicitor.ask(Question("contributions", "4) 主要贡献有哪些？（分号分隔，可留空）"))
    )
    novelty = _split_items(
        elicitor.ask(
            Question("novelty", "5) 关键新颖性/与已有工作的区别？（分号分隔，可留空）")
        )
    )

    return build_artifact_from_description(
        field=field_,
        problem=problem,
        method=method,
        contributions=contributions,
        novelty_claims=novelty,
    )


__all__ = ["build_artifact_from_description", "run_intake"]
