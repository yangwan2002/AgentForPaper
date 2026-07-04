"""有界 ReAct 工具循环（升级：历史压缩 + 结果截断 + 真实 token 计量）。

模型在生成过程中可自主发起工具调用（function calling）：
  调模型 → 若请求工具 → 执行工具 → 把结果回灌 → 再调模型 → … 直到模型给出
  最终答案，或达到最大轮数（防止失控）。

这是把"pipeline 中的一个节点"升级为"小型自主 agent"的通用机制，
被写作智能体用于"写作时按需检索/核验文献"（Req 4.4）。

本次升级（Req 8 / 7.6 / 7.7 / 10.7）解决两个问题：
- 无界追加消息导致上下文溢出 → 每轮前用 `TokenCounter` 计量，超
  `context_token_budget` 时压缩历史（保留全部系统提示 + 最近
  `keep_recent_turns` 轮原文 + 旧轮折叠为单条摘要）。
- `str(result)` 原样塞入导致单条工具结果撑爆上下文 → 用
  `truncate_to_tokens` 截断超 `max_tool_result_tokens` 的结果并附带含原始
  token 数的截断备注。

工具结果与 LLM 输出均被视为不可信数据：截断与渲染都做防御式处理，
不执行 `eval`/`exec`，并对单条结果附加硬字符上限（Req 10.7）。
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace

from paper_agent.context.tokenizer import TokenCounter, build_token_counter
from paper_agent.providers.llm.base import LLMProvider, Message
from paper_agent.tools.registry import ToolRegistry

# 不可信工具结果的防御式硬字符上限（Req 10.7：保留不超过 8000 字符）。
_MAX_TOOL_RESULT_CHARS = 8000


@dataclass
class ToolLoopConfig:
    """工具循环的可调参数（Req 8）。

    取值范围见需求文档；此处仅给默认值，越界校验由装配层负责。
    """

    max_iters: int = 4                   # 最大轮数（1..50）
    context_token_budget: int = 8000     # 触发历史压缩的累计 token 阈值（1..1_000_000）
    max_tool_result_tokens: int = 1000   # 单个工具结果截断上限（100..100_000）
    keep_recent_turns: int = 2           # 压缩时保留最近 N 轮原文（1..50）


@dataclass
class ToolLoopResult:
    content: str
    tool_calls_made: int = 0
    logs: list[str] = field(default_factory=list)


def _render_for_summary(messages: list[Message]) -> str:
    """把一组待压缩的旧消息渲染为可供 LLM 摘要的纯文本。

    防御式处理：容错缺失字段，工具调用按「名称(参数)」紧凑呈现。
    """
    lines: list[str] = []
    for m in messages:
        role = getattr(m, "role", "") or ""
        content = getattr(m, "content", "") or ""
        parts = [f"[{role}] {content}".rstrip()]
        for call in getattr(m, "tool_calls", None) or []:
            name = getattr(call, "name", "") or ""
            args = getattr(call, "arguments", "") or ""
            parts.append(f"  ↳ 调用工具 {name}({args})")
        lines.append("\n".join(parts))
    return "\n".join(lines)


def _summarize(llm: LLMProvider, text: str) -> str:
    """用 LLM 将早前对话压缩为简洁要点（类似 ContextManager.summarize）。

    防御式：摘要失败时回退为对原文的截断，绝不抛出以免中断工具循环。
    """
    messages = [
        Message(
            role="system",
            content=(
                "你是上下文压缩助手。请把以下早前的工具循环对话压缩为简洁要点，"
                "务必保留：已检索到的文献与关键结论、已确定的约束与决策、尚未完成"
                "的待办。只输出摘要正文，不要解释。"
            ),
        ),
        Message(role="user", content=text),
    ]
    try:
        resp = llm.complete(messages)
        digest = (resp.content or "").strip()
        return digest or text[:2000]
    except Exception:  # noqa: BLE001 - 摘要失败不应中断主循环
        return text[:2000]


def truncate_to_tokens(
    text: str,
    max_tokens: int,
    counter: TokenCounter,
    note: str,
) -> str:
    """把文本截断到不超过 `max_tokens` 个 token，截断时在其后附加 `note`。

    Preconditions：`max_tokens > 0`。
    Postconditions：
    - 未截断（原文 token 数 ≤ max_tokens）时原样返回；
    - 截断时 `counter.count(返回值) <= max_tokens + counter.count(note)`。

    实现：以字符前缀二分查找「token 数 ≤ max_tokens」的最长前缀（无需反向
    解码），再叠加防御式硬字符上限（Req 10.7），最后拼接备注。拼接依赖
    计数器的次可加性（heuristic 与 tiktoken 均满足）保证上界成立。
    """
    if max_tokens <= 0:
        # 防御式：非法预算时退化为仅保留备注。
        return note
    if counter.count(text) <= max_tokens:
        return text

    # 二分查找 token 数不超过预算的最长字符前缀。
    lo, hi, best = 0, len(text), 0
    while lo <= hi:
        mid = (lo + hi) // 2
        if counter.count(text[:mid]) <= max_tokens:
            best = mid
            lo = mid + 1
        else:
            hi = mid - 1

    # 叠加防御式硬字符上限（不可信数据，Req 10.7）。
    best = min(best, _MAX_TOOL_RESULT_CHARS)
    head = text[:best]
    return head + note


def compact_history(
    messages: list[Message],
    counter: TokenCounter,
    config: ToolLoopConfig,
    summarizer,
) -> list[Message]:
    """压缩历史：保留全部系统提示 + 最近 keep_recent_turns 轮原文，旧轮折叠为单条摘要。

    返回新列表；当没有可折叠的旧轮时原样返回入参（无副作用）。

    不变式：返回后语义关键信息（已检索文献、约束、决策）经摘要保留，
    且消息条数显著下降。
    """
    system = [m for m in messages if getattr(m, "role", None) == "system"]
    body = [m for m in messages if getattr(m, "role", None) != "system"]

    keep = max(0, config.keep_recent_turns) * 2
    recent = body[len(body) - keep:] if keep else []
    old = body[: len(body) - len(recent)]
    if not old:
        # 没有可折叠的旧轮（如历史过短），保持原样。
        return messages

    digest = summarizer(_render_for_summary(old))
    summary_msg = Message(role="system", content=f"[早前对话摘要] {digest}")
    return system + [summary_msg] + recent


def run_tool_loop(
    llm: LLMProvider,
    messages: list[Message],
    registry: ToolRegistry,
    *,
    counter: TokenCounter | None = None,
    config: ToolLoopConfig | None = None,
    max_iters: int | None = None,
    **complete_opts,
) -> ToolLoopResult:
    """运行有界工具循环，返回模型最终正文（Req 8）。

    messages 会被原地修改（压缩、追加 assistant 工具请求与 tool 结果），
    调用方如需保留原始消息应传入副本。

    向后兼容：`counter` 缺省时构造统一的 `TokenCounter`；`config` 缺省时用
    默认 `ToolLoopConfig`；旧式关键字 `max_iters` 仍被接受并折叠进 config。
    """
    if config is None:
        config = ToolLoopConfig()
    if max_iters is not None:
        # 兼容旧调用方 run_tool_loop(..., max_iters=N)。
        config = replace(config, max_iters=max_iters)
    if counter is None:
        counter = build_token_counter()

    schemas = registry.to_openai_schemas()
    logs: list[str] = []
    calls_made = 0
    incompressible_logged = False

    def _summarizer(text: str) -> str:
        return _summarize(llm, text)

    for _ in range(config.max_iters):
        # 不变式：进入每轮前，messages 的 token 数 ≤ 预算（必要时已压缩）。
        if counter.count_messages(messages) > config.context_token_budget:
            messages[:] = compact_history(messages, counter, config, _summarizer)
            if (
                not incompressible_logged
                and counter.count_messages(messages) > config.context_token_budget
            ):
                logs.append(
                    "历史已达不可压缩下限，继续以当前消息列表调用 LLM"
                )
                incompressible_logged = True

        resp = llm.complete(messages, tools=schemas, **complete_opts)
        if not resp.tool_calls:
            return ToolLoopResult(
                content=resp.content, tool_calls_made=calls_made, logs=logs
            )

        # 回灌 assistant 的工具请求消息。
        messages.append(
            Message(
                role="assistant",
                content=resp.content or "",
                tool_calls=resp.tool_calls,
            )
        )
        for call in resp.tool_calls:
            calls_made += 1
            try:
                result = registry.call(call.name, **call.arguments)
                result_text = str(result)
                logs.append(f"工具调用 {call.name}({call.arguments}) → 成功")
            except Exception as exc:  # noqa: BLE001 - 工具错误回灌给模型自纠
                # Req 8.7：错误文本作为对应 tool_call_id 的结果回灌并继续。
                result_text = f"工具执行失败：{exc}"
                logs.append(f"工具调用 {call.name} 失败：{exc}")

            # Req 8.5/8.6：超长结果（含错误文本）截断并附含原始 token 数的备注。
            original_tokens = counter.count(result_text)
            note = (
                f"\n\n[结果过长已截断：原始 {original_tokens} tokens，"
                f"保留前约 {config.max_tool_result_tokens} tokens，"
                f"可用 read_section/read_reference 等工具按需取全文]"
            )
            result_text = truncate_to_tokens(
                result_text, config.max_tool_result_tokens, counter, note=note
            )
            messages.append(
                Message(role="tool", content=result_text, tool_call_id=call.id)
            )

    # 超出最大轮数：去掉工具、强制模型给出最终答案（Req 8.8）。
    final = llm.complete(messages, **complete_opts)
    logs.append(f"达到工具调用上限（{config.max_iters} 轮），强制收尾")
    return ToolLoopResult(
        content=final.content, tool_calls_made=calls_made, logs=logs
    )
