"""写作期"按需向用户提问"工具（mid-loop ask_user）。

动机：很多信息缺口只有写到具体章节才暴露——缺失的实验数字、未定义的术语、
某条声明缺具体引用来源。这类"只有作者才知道、且答案会实质改变本节内容"的缺口，
非交互管线只能靠质量闸/忠实性审计**拒绝或删除**，无法**向用户要到**那条信息。
本工具让写作智能体在其工具循环里按需 `ask_user`，把决策交还作者。

安全约束（与既有工具契约一致）：
- **仅交互式 Elicitor 才注册此工具**（非交互下写作智能体根本不暴露它）；
- **配额上限** ``budget``：单次运行内提问次数封顶，防写作期狂问；
- **答案缓存/持久化**：按问题文本哈希缓存；命中直接返回不再问——既在同一运行内
  去重，又支持续跑（种子来自 ``ws.profile['clarification_answers']``）回放、不重复问；
- **工具不直接写工作区**：新答案由写作智能体经 ``AgentResult.mutations`` 单一写入路径
  落盘（本工具只累积，见 ``collected`` / ``persist_mutation``）。
"""

from __future__ import annotations

import hashlib

from paper_agent.agents.base import AgentResult, WorkspaceMutation
from paper_agent.elicitation import Elicitor, Question
from paper_agent.workspace.models import PaperWorkspace

# ask_user 工具的 function calling schema。
_ASK_USER_SCHEMA = {
    "type": "object",
    "properties": {
        "question": {
            "type": "string",
            "description": "要问作者的具体问题；答案应会实质改变本节内容"
            "（如缺失的实验数字、未定义术语、某声明的具体引用来源）。",
        },
        "options": {
            "type": "string",
            "description": "可选：用竖线 | 分隔的候选答案，便于作者快速选择。",
        },
    },
    "required": ["question"],
}

_ASK_USER_DESCRIPTION = (
    "向论文作者提出一个澄清问题并获得回答。仅在你遇到**只有作者才知道、且答案会"
    "实质改变本节内容**的信息缺口时才调用（如缺失的真实实验数字、未定义的专有术语、"
    "某条 claim 缺具体引用来源）。不要用它问你能自行合理决定的事。若返回提示用户"
    "不可用或已达上限，请基于现有信息自行合理处理，切勿编造数字或引用。"
)


def _key(question: str) -> str:
    return hashlib.sha1(question.strip().encode("utf-8")).hexdigest()[:16]


class AskUserTool:
    """写作期 ask_user 工具：配额 + 缓存 + 非交互守卫，累积答案供单一写入路径落盘。"""

    def __init__(
        self,
        elicitor: Elicitor,
        existing_answers: list[dict] | None = None,
        *,
        budget: int = 3,
    ) -> None:
        self._elicitor = elicitor
        self._budget = max(0, int(budget))
        self._asks_used = 0
        # key -> {"question", "answer"}：种子为已持久化答案（续跑回放、跨节去重）。
        self._cache: dict[str, dict] = {}
        for rec in existing_answers or []:
            q = str(rec.get("question", "")).strip()
            if q:
                self._cache[_key(q)] = {"question": q, "answer": str(rec.get("answer", ""))}
        self._new: dict[str, dict] = {}

    def ask(self, question: str, options: str = "") -> str:
        """工具处理器：向用户提问并返回答案（命中缓存/超额/非交互时返回提示）。"""
        q = (question or "").strip()
        if not q:
            return "错误：ask_user 需要非空的 question。"
        key = _key(q)
        if key in self._cache:
            return f"（已采用作者此前的回答）{self._cache[key]['answer']}"
        if not getattr(self._elicitor, "interactive", False):
            return "作者当前不可用（非交互模式）。请基于现有信息自行合理处理，不要编造。"
        if self._asks_used >= self._budget:
            return "已达本轮向作者提问的上限。请基于现有信息自行合理处理，不要编造。"

        opts = [o.strip() for o in options.split("|") if o.strip()] if options else []
        self._asks_used += 1
        ans = (self._elicitor.ask(Question(id=key, prompt=q, options=opts, default="")) or "").strip()
        if not ans:
            return "作者未提供该信息。请勿编造；可如实留待补充，或改写以不依赖该信息。"
        rec = {"question": q, "answer": ans}
        self._cache[key] = rec
        self._new[key] = rec
        return ans

    @property
    def collected(self) -> dict[str, dict]:
        """本次运行新收集到的问答（供写作智能体持久化）。"""
        return self._new

    def persist_mutation(self) -> WorkspaceMutation:
        """返回一个把新问答并入 ``ws.profile['clarification_answers']`` 的更新意图。"""
        new_records = list(self._new.values())

        def mutate(w: PaperWorkspace) -> None:
            existing = list(w.profile.get("clarification_answers") or [])
            seen = {c.get("question") for c in existing}
            for rec in new_records:
                if rec["question"] not in seen:
                    existing.append(rec)
                    seen.add(rec["question"])
            w.profile["clarification_answers"] = existing

        return mutate


def register_ask_user_tool(registry, tool: AskUserTool) -> None:
    """把 ask_user 工具注册进给定 registry（仅在交互模式下由写作智能体调用）。"""
    registry.register(
        name="ask_user",
        description=_ASK_USER_DESCRIPTION,
        handler=tool.ask,
        parameters=_ASK_USER_SCHEMA,
    )


__all__ = ["AskUserTool", "register_ask_user_tool"]
