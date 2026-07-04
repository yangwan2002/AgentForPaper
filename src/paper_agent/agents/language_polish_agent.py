"""语言润色 / 一致性校对智能体（投递前的独立语言 pass）。

动机：此前语言质量只由评审四维之一（language）以 LLM 自评兜底，没有专门的
润色环节，长论文容易出现术语不统一、中英混排、语病与套话。本智能体在反馈循环
**收敛后、导出前**运行一次：逐章节做纯语言层面的改写，提升可读性与一致性，同时
**严格保真事实、数据、引用与结构**。

安全契约（与既有 grounding / 单一写入路径一致）：
- 只做语言改写，绝不新增/删除/改动数字与方括号 ``[id]`` 引用标注；
- 润色后经确定性守卫校验——若引用集合被破坏、出现新数字、或长度异常膨胀/塌缩，
  则**丢弃润色结果、保留原文**（宁可不润色也不破坏内容）；
- Mock/测试 provider（``is_mock=True``）下整体 no-op，输出逐字节不变，保证既有
  基于 Mock 的测试与「停用等价」语义；
- 所有工作区写入经 ``AgentResult.mutations`` 走 ``WorkspaceRepository`` 单一写入路径。
"""

from __future__ import annotations

from paper_agent.agents.base import Agent, AgentContext, AgentResult
from paper_agent.prompts import templates
from paper_agent.providers.llm.base import LLMProvider
from paper_agent.tools import polish_guards
from paper_agent.workspace.models import PaperWorkspace


class LanguagePolishAgent(Agent):
    name = "language_polish_agent"

    def __init__(
        self,
        llm: LLMProvider,
        *,
        is_mock: bool = False,
        enabled: bool = True,
    ) -> None:
        self._llm = llm
        self._is_mock = is_mock
        self._enabled = enabled

    def run(self, ctx: AgentContext) -> AgentResult:
        ws = ctx.workspace
        # 停用或 Mock provider：整体 no-op（逐字节不变，向后兼容）。
        if not self._enabled or self._is_mock:
            return AgentResult()
        if not ws.section_drafts:
            return AgentResult()

        from paper_agent.prompts.section_types import infer_and_get_spec

        glossary_terms = self._glossary_block(ws)
        polished: dict[str, str] = {}
        logs: list[str] = []
        for node in ws.ordered_sections():
            draft = ws.section_drafts.get(node.section_id)
            if draft is None or not draft.content.strip():
                continue
            # 按章节体裁注入语言/结构惯例（Round 8）；守卫仍兜底防越界改动。
            guidance = infer_and_get_spec(node.section_id, node.title).writing_guidance
            new_content = self._polish_one(
                node.title, draft.content, glossary_terms, guidance
            )
            if new_content is not None and new_content != draft.content:
                polished[node.section_id] = new_content

        if not polished:
            return AgentResult(logs=["语言润色：无改动（守卫拦截或无变化）"])

        def mutate(w: PaperWorkspace) -> None:
            for sid, content in polished.items():
                if sid in w.section_drafts:
                    w.section_drafts[sid].content = content

        logs.append(f"语言润色：改写 {len(polished)} 个章节（已通过保真守卫）")
        return AgentResult(mutations=[mutate], logs=logs)

    # --- 内部 ---

    def _polish_one(
        self,
        title: str,
        content: str,
        glossary_terms: str,
        section_guidance: str = "",
    ) -> str | None:
        """润色单个章节；返回通过守卫的润色文本，否则返回 None（保留原文）。"""
        try:
            resp = self._llm.complete(
                templates.polish_section(
                    title=title,
                    content=content,
                    glossary_terms=glossary_terms,
                    section_guidance=section_guidance,
                )
            )
        except Exception:  # noqa: BLE001 - 润色失败不阻断管线，保留原文
            return None
        candidate = (resp.content or "").strip()
        if not candidate:
            return None
        if not self._passes_guards(content, candidate):
            return None
        return candidate

    @staticmethod
    def _passes_guards(original: str, candidate: str) -> bool:
        """确定性保真守卫：引用集合不变、数值集合不变、长度浮动在允许区间内。"""
        if not candidate.strip():
            return False
        return (
            polish_guards.content_preserved(original, candidate)
            and polish_guards.length_ratio_ok(original, candidate)
        )

    @staticmethod
    def _glossary_block(ws: PaperWorkspace) -> str:
        """把工作区术语表渲染为「统一用词」提示（空则返回空串）。"""
        if not ws.glossary:
            return ""
        return "\n".join(f"- {term}：{definition}" for term, definition in ws.glossary.items())


__all__ = ["LanguagePolishAgent"]
