"""上下文管理模块（Req 5.2 / 5.3 / 5.5）。

借鉴通用智能体的上下文压缩思想，为写作智能体准备「注入上下文」：
- 全局大纲 + 已完成章节摘要（而非全文），避免长论文撑爆上下文。
- 术语表注入，保持术语一致。
- 按 token 预算裁剪，优先保留与当前章节强相关的摘要。

token 计量采用注入的 `TokenCounter`（真实分词器 + 启发式回退），统一全局口径。
"""

from __future__ import annotations

from dataclasses import dataclass

from paper_agent.context.tokenizer import TokenCounter, build_token_counter
from paper_agent.prompts import templates
from paper_agent.profile import render_profile
from paper_agent.providers.llm.base import LLMProvider
from paper_agent.workspace.models import PaperWorkspace

# token 预算取值范围（Req 7.4）。
_MIN_TOKEN_BUDGET = 1
_MAX_TOKEN_BUDGET = 200000


def estimate_tokens(text: str) -> int:
    """粗略估算 token 数：中英文混合按 ~2 字符/token 近似。

    注意：此函数仅保留作启发式参考与历史兼容；`ContextManager` 的裁剪现已
    改用注入的 `TokenCounter`。
    """
    return max(1, len(text) // 2)


@dataclass
class ContextBlock:
    outline: str
    summaries: str
    glossary: str

    def render(self) -> str:
        return (
            f"大纲：{self.outline}\n"
            f"已完成章节摘要：{self.summaries}\n"
            f"术语表：{self.glossary}"
        )


class ContextManager:
    def __init__(
        self,
        llm: LLMProvider,
        token_budget: int = 1500,
        *,
        counter: TokenCounter | None = None,
    ) -> None:
        self._llm = llm
        # 预算钳制到合法范围 [1, 200000]（Req 7.4）。
        self._budget = max(_MIN_TOKEN_BUDGET, min(_MAX_TOKEN_BUDGET, token_budget))
        # 未注入时构造默认计数器，保证 ContextManager(llm) 仍可用（向后兼容）。
        self._counter = counter if counter is not None else build_token_counter()

    def build_context(
        self, ws: PaperWorkspace, current_section_id: str
    ) -> ContextBlock:
        outline = "；".join(n.title for n in ws.ordered_sections())
        # 术语按 key 排序渲染：使前缀逐字节稳定、不受 dict 插入顺序抖动影响，
        # 提升服务端前缀缓存命中率（省 token）。
        glossary = "；".join(f"{k}={v}" for k, v in sorted(ws.glossary.items()))
        summaries = self._select_summaries(ws, current_section_id)
        return ContextBlock(outline=outline, summaries=summaries, glossary=glossary)

    def stable_block(self, ws: PaperWorkspace) -> str:
        """运行内稳定的上下文（论文档案 + 大纲 + 术语表），用作可缓存前缀的一部分。

        注意：此处不含每节变化的摘要，以保证前缀逐字节稳定。

        #13：该稳定段作为 prompt 的第二段（system 之后、task 之前）注入，
        在 OpenAI 兼容服务端会自动命中前缀缓存（prefix caching）——无需显式
        ``cache_control``（那属 Anthropic 原生协议）。故"逐字节稳定可缓存"的
        设计意图在 OpenAI 兼容路径下已落地：稳定前缀复用、降低 token 成本。
        """
        outline = "；".join(n.title for n in ws.ordered_sections())
        # 术语按 key 排序，保证稳定前缀（前缀缓存友好，#13）。
        glossary = "；".join(f"{k}={v}" for k, v in sorted(ws.glossary.items()))
        parts = []
        profile_text = render_profile(ws.profile)
        if profile_text:
            parts.append(profile_text)
        parts.append(f"[全局大纲] {outline}")
        parts.append(f"[术语表] {glossary or '（无）'}")
        return "\n".join(parts)

    def summaries_block(self, ws: PaperWorkspace, current_section_id: str) -> str:
        """本次易变的已写章节摘要（按预算裁剪），放入 task 段。"""
        return self._select_summaries(ws, current_section_id)

    def _select_summaries(self, ws: PaperWorkspace, current_section_id: str) -> str:
        """按预算挑选已完成章节摘要，优先邻近当前章节的章节。"""
        ordered = ws.ordered_sections()
        order_index = {n.section_id: n.order for n in ordered}
        current_order = order_index.get(current_section_id, 0)

        items = [
            (sid, summary)
            for sid, summary in ws.section_summaries.items()
            if sid != current_section_id and summary
        ]
        # 距离当前章节越近优先级越高。
        items.sort(key=lambda kv: abs(order_index.get(kv[0], 999) - current_order))

        selected: list[str] = []
        used = 0
        for sid, summary in items:
            piece = f"{sid}:{summary}"
            cost = self._counter.count(piece)
            if used + cost > self._budget:
                break
            selected.append(piece)
            used += cost
        return "；".join(selected)

    def summarize_section(self, title: str, content: str) -> str:
        """为已完成章节生成摘要（Req 5.2）。"""
        resp = self._llm.complete(
            templates.summarize_section(title=title, content=content)
        )
        return resp.content.strip()
