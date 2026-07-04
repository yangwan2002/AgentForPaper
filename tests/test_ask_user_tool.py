"""写作期 ask_user 工具测试：非交互守卫、配额、缓存/去重、持久化、注册暴露。"""

from __future__ import annotations

from paper_agent.elicitation import AutoElicitor, ScriptedElicitor
from paper_agent.tools.ask_user_tool import AskUserTool, register_ask_user_tool
from paper_agent.tools.registry import ToolRegistry
from paper_agent.workspace.models import InputMode, PaperWorkspace


class _CountingElicitor(ScriptedElicitor):
    def __init__(self, answers):
        super().__init__(answers)
        self.calls = 0

    def ask(self, question):
        self.calls += 1
        return super().ask(question)


def test_non_interactive_never_prompts():
    tool = AskUserTool(AutoElicitor(), budget=3)
    out = tool.ask("需要一个数字")
    assert "非交互" in out
    assert tool.collected == {}


def test_ask_returns_answer_and_collects():
    e = ScriptedElicitor({})  # 未命中 id → default ""? 需按 id 应答
    # ask_user 用问题哈希作 id，测试用队列式应答更方便。
    e = ScriptedElicitor(["42.0"])
    tool = AskUserTool(e, budget=3)
    ans = tool.ask("方法一节缺一个准确率数字，是多少？")
    assert ans == "42.0"
    assert len(tool.collected) == 1


def test_cache_dedups_same_question():
    e = _CountingElicitor(["answer-1"])
    tool = AskUserTool(e, budget=5)
    q = "同一个问题"
    a1 = tool.ask(q)
    a2 = tool.ask(q)  # 第二次应命中缓存，不再问
    assert a1 == "answer-1"
    assert "已采用作者此前的回答" in a2
    assert e.calls == 1  # 只真问了一次


def test_budget_enforced():
    e = _CountingElicitor(["a", "b", "c", "d"])
    tool = AskUserTool(e, budget=2)
    assert tool.ask("q1") == "a"
    assert tool.ask("q2") == "b"
    out = tool.ask("q3")  # 超额
    assert "上限" in out
    assert e.calls == 2


def test_seeded_from_existing_answers_replays():
    existing = [{"question": "旧问题", "answer": "旧答案"}]
    e = _CountingElicitor(["不应被用到"])
    tool = AskUserTool(e, existing_answers=existing, budget=3)
    out = tool.ask("旧问题")
    assert "旧答案" in out
    assert e.calls == 0  # 续跑回放，不重复问


def test_empty_answer_warns_no_fabrication():
    e = ScriptedElicitor([""])  # 用户回车留空
    tool = AskUserTool(e, budget=3)
    out = tool.ask("你有这个数字吗？")
    assert "未提供" in out
    assert tool.collected == {}  # 空答案不记录


def test_persist_mutation_merges_into_profile():
    e = ScriptedElicitor(["95.6"])
    tool = AskUserTool(e, budget=3)
    tool.ask("准确率是多少？")
    ws = PaperWorkspace(workspace_id="w", input_mode=InputMode.DRAFT_REVISION)
    ws.profile["clarification_answers"] = [{"question": "旧", "answer": "老"}]
    tool.persist_mutation()(ws)
    qs = [c["question"] for c in ws.profile["clarification_answers"]]
    assert "旧" in qs and "准确率是多少？" in qs


def test_register_exposes_schema():
    reg = ToolRegistry()
    tool = AskUserTool(ScriptedElicitor([]), budget=3)
    register_ask_user_tool(reg, tool)
    schemas = reg.to_openai_schemas()
    names = {s["function"]["name"] for s in schemas}
    assert "ask_user" in names


def test_writing_agent_gates_ask_tool_on_interactivity():
    from paper_agent.agents.writing_agent import WritingAgent
    from paper_agent.providers.llm.mock import MockLLMProvider

    ws = PaperWorkspace(workspace_id="w", input_mode=InputMode.DRAFT_REVISION)

    # 非交互（无 elicitor）→ 不构造 ask 工具。
    agent_auto = WritingAgent(MockLLMProvider())
    assert agent_auto._make_ask_tool(ws) is None

    # 交互式 → 构造 AskUserTool。
    agent_int = WritingAgent(MockLLMProvider(), elicitor=ScriptedElicitor([]))
    assert agent_int._make_ask_tool(ws) is not None
