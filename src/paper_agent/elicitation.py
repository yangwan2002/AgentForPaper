"""用户澄清问答（human-in-the-loop）：`Elicitor` 抽象 + 三种实现。

当 agent 对"下一步该怎么做"拿不准时（例如初稿只有方法/实验、缺引言），不应擅自
猜测，而应把问题抛给用户、拿到答案再继续——借鉴 Claude / Cursor 的澄清式交互。

本模块提供与 `EventSink`（只出）对称的"只进"通道：`Elicitor.ask(question)`。
依赖注入，便于替换与测试：

- ``CLIElicitor``：终端交互（`input`/`print`），真实使用。
- ``ScriptedElicitor``：测试注入固定答案（按 id 或顺序），保证确定可测。
- ``AutoElicitor``：非交互/CI/Mock 场景，一律返回每个问题的**默认答案**——不阻塞、
  行为确定，使既有批处理与测试逐字节不变。

所有实现只做纯 I/O，不调用 LLM。答案由调用方记录进工作区以保证可复现与续跑。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Protocol, runtime_checkable


@dataclass
class Question:
    """一个待澄清的问题。

    Attributes:
        id: 稳定标识（用于记录答案、脚本化测试按 id 应答）。
        prompt: 展示给用户的问题文本。
        options: 可选项列表；为空表示自由文本。
        default: 非交互/未答/空输入时采用的答案（必须是 options 之一或自由文本）。
    """

    id: str
    prompt: str
    options: list[str] = field(default_factory=list)
    default: str = ""


@runtime_checkable
class Elicitor(Protocol):
    # 是否为交互式（真会向用户要输入）。非交互实现为 False，供调用方决定是否
    # 值得为"动态提问"花额外的 LLM 调用（非交互下直接跳过）。
    interactive: bool

    def ask(self, question: Question) -> str:
        """就 ``question`` 征询用户，返回其答案（可能为 ``default``）。"""
        ...

    def ask_batch(self, questions: list[Question]) -> dict[str, str]:
        """一次性征询一组问题，返回 ``{question.id: answer}``。

        用于「一屏问完 3-5 个关键问题」而非一步一停的澄清体验。默认实现按序调用
        ``ask``；交互实现可在此打印统一抬头。"""
        ...


class _AskBatchMixin:
    """``ask_batch`` 的默认实现：按序调用 ``ask`` 并聚合为 ``{id: answer}``。"""

    def ask_batch(self, questions: list[Question]) -> dict[str, str]:
        return {q.id: self.ask(q) for q in questions}


class AutoElicitor(_AskBatchMixin):
    """非交互实现：一律返回问题的默认答案（不阻塞、确定）。"""

    interactive = False

    def ask(self, question: Question) -> str:
        return question.default


class ScriptedElicitor(_AskBatchMixin):
    """测试实现：按 id 映射或按顺序返回预置答案；耗尽/未命中回落 default。"""

    interactive = True

    def __init__(self, answers: dict[str, str] | list[str] | None = None) -> None:
        self._by_id: dict[str, str] = {}
        self._queue: list[str] = []
        if isinstance(answers, dict):
            self._by_id = dict(answers)
        elif isinstance(answers, list):
            self._queue = list(answers)

    def ask(self, question: Question) -> str:
        if question.id in self._by_id:
            return self._by_id[question.id]
        if self._queue:
            return self._queue.pop(0)
        return question.default


class CLIElicitor(_AskBatchMixin):
    """终端交互实现：打印问题（含选项与默认），读取一行；空输入采用默认。

    I/O 经参数注入，便于测试。选项存在时：接受序号（1 起）或选项文本；无法识别
    的输入按自由文本原样返回（对无选项问题即为答案本身）。
    """

    interactive = True

    def __init__(
        self,
        input_fn: Callable[[str], str] = input,
        output_fn: Callable[[str], None] = print,
    ) -> None:
        self._input = input_fn
        self._output = output_fn

    def ask_batch(self, questions: list[Question]) -> dict[str, str]:
        """一次性问一组问题：先打印统一抬头，再逐题询问（一屏问完的体验）。"""
        if questions:
            self._output(
                f"\n系统需要确认 {len(questions)} 个问题以决定如何处理你的稿件："
            )
        return {q.id: self.ask(q) for q in questions}

    def ask(self, question: Question) -> str:
        self._output(question.prompt)
        if question.options:
            for i, opt in enumerate(question.options, start=1):
                marker = "（默认）" if opt == question.default else ""
                self._output(f"  {i}. {opt}{marker}")
        elif question.default:
            self._output(f"（直接回车采用默认：{question.default}）")

        try:
            raw = self._input("> ").strip()
        except EOFError:
            return question.default
        if not raw:
            return question.default
        if question.options:
            # 序号选择。
            if raw.isdigit():
                idx = int(raw) - 1
                if 0 <= idx < len(question.options):
                    return question.options[idx]
            # 文本精确匹配（忽略大小写）。
            for opt in question.options:
                if raw.lower() == opt.lower():
                    return opt
            # 无法识别 → 回落默认，避免把乱输入当答案。
            return question.default
        return raw


__all__ = [
    "Question",
    "Elicitor",
    "AutoElicitor",
    "ScriptedElicitor",
    "CLIElicitor",
]

