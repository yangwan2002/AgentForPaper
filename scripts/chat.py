"""交互式论文写作助手 CLI（路径 A）——多轮对话，像简版 Claude Code。

用法：
    python scripts/chat.py                       # 直接进入对话（无初稿/主题）
    python scripts/chat.py my_draft.tex          # 带初稿进入对话
    python scripts/chat.py --topic "图神经网络"   # 带主题进入对话
    python scripts/chat.py --resume <session_id> # 续跑既有会话

进入后直接用自然语言下达任务，例如：
    "帮我把实验章节的叙述改简洁"
    "给相关工作补 5 篇近三年的文献"
输入 /exit 退出，/files 看产出文件，/help 看命令。

依赖 .env 中的 PAPER_LLM / PAPER_BASE_URL / PAPER_LLM_MODEL / PAPER_KEY_ENV。
"""

from __future__ import annotations

import argparse
import os
import sys

# Windows 控制台默认 GBK，打印部分字符会崩；切到 UTF-8（容错替换）。
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:  # noqa: BLE001
    pass

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from paper_agent.agent_platform.app import build_agent_app  # noqa: E402
from paper_agent.agent_platform.chat import run_chat_repl  # noqa: E402
from paper_agent.agent_platform.models import WritingTask  # noqa: E402
from paper_agent.config import Config  # noqa: E402
from paper_agent.observability.usage import UsageTracker  # noqa: E402
from paper_agent.utils.dotenv import load_dotenv  # noqa: E402
from paper_agent.workspace.models import OutputFormat  # noqa: E402


def _build_config() -> Config:
    return Config(
        llm_provider=os.environ.get("PAPER_LLM", "anthropic"),
        llm_model=os.environ.get("PAPER_LLM_MODEL", ""),
        llm_base_url=os.environ.get("PAPER_BASE_URL") or None,
        llm_api_key_env=os.environ.get("PAPER_KEY_ENV") or None,
        retrieval_provider=os.environ.get("PAPER_RETRIEVAL", "mock"),
        workspace_dir=os.environ.get("PAPER_WORKSPACE_DIR", "output"),
        default_output_format=OutputFormat.MARKDOWN,
        allow_self_review=os.environ.get("PAPER_ALLOW_SELF_REVIEW", "0") == "1",
        wall_clock_deadline_s=float(os.environ.get("PAPER_DEADLINE_S", "1200")),
        total_token_budget=int(os.environ.get("PAPER_TOKEN_BUDGET", "0")),
        # 追踪落盘默认开启：每轮对话的所有事件按 trace 落到 <workspace>/traces，
        # 供出问题时用 scripts/trace_view.py 定位是哪一步。设 PAPER_TRACING=0 关闭。
        tracing_enabled=os.environ.get("PAPER_TRACING", "1") != "0",
        trace_content_level=os.environ.get("PAPER_TRACE_LEVEL", "full"),
        # 通用代码执行工具 run_python（低风险长尾：拼图/插图/调段落格式等）默认开启，
        # 用操作系统子进程隔离（锁工作目录 + 超时；本机单人够用，隔离弱）。
        # 设 PAPER_RUN_PYTHON=0 关闭；PAPER_SANDBOX_BACKEND=docker 可切强隔离（需 Docker）。
        run_python_enabled=os.environ.get("PAPER_RUN_PYTHON", "1") != "0",
        sandbox_backend=os.environ.get("PAPER_SANDBOX_BACKEND", "subprocess"),
    )


def _parse_args(argv=None):
    p = argparse.ArgumentParser(description="交互式论文写作助手（多轮对话）")
    p.add_argument("input", nargs="?", help="初稿文件路径（可选）")
    p.add_argument("--topic", help="论文主题（无初稿时可用）")
    p.add_argument("--resume", help="续跑既有会话 id")
    return p.parse_args(argv)


def _check_retrieval_or_exit(config) -> None:
    """生产入口强制真实文献检索（P0）：mock 会返回假文献、误导用户。

    检索为 mock 且未显式设 ``PAPER_ALLOW_MOCK_RETRIEVAL=1`` 时 fail-fast 退出，
    避免用户以为"加文献"用了真实文献库。本地无网调试可显式开启 mock。
    """
    if config.retrieval_provider.lower() != "mock":
        return
    if os.environ.get("PAPER_ALLOW_MOCK_RETRIEVAL") == "1":
        print(
            "⚠ 文献检索为 mock（离线，仅供调试）：add_references 不会返回真实文献。",
            file=sys.stderr,
        )
        return
    print(
        "错误：当前文献检索为 mock（离线假数据），会误导「加文献」类任务。\n"
        "请设置真实检索后重试，例如：\n"
        "    set PAPER_RETRIEVAL=openalex   （或 arxiv / api）\n"
        "如确需离线调试，显式设置：set PAPER_ALLOW_MOCK_RETRIEVAL=1",
        file=sys.stderr,
    )
    sys.exit(2)


def _on_tool_call(name: str, args: dict) -> None:
    """实时展示 agent 的工具调用（Claude Code 式的可见性）。

    流式输出进行中光标在行内，先换行再打印工具行，避免与流式文本粘连。
    """
    print(f"\n  · [工具] {name}", flush=True)


def main(argv=None) -> None:
    load_dotenv()
    args = _parse_args(argv)

    from paper_agent.agent_platform.chat import StreamingChatSink
    from paper_agent.agent_platform.models import TaskAgentConfig
    from paper_agent.elicitation import CLIElicitor

    config = _build_config()
    _check_retrieval_or_exit(config)
    tracker = UsageTracker()
    # 单轮工具调用上限（大任务可经 PAPER_MAX_ITERS 调高）。撞上限时 REPL 会自动续跑，
    # 无需手动敲「继续」；真正的资源闸是 token 预算与墙钟时间。
    agent_config = TaskAgentConfig(max_iters=int(os.environ.get("PAPER_MAX_ITERS", "60")))
    # 流式 sink：把 LLM 内容增量实时写到终端（边生成边显示）。
    sink = StreamingChatSink()
    # 交互式：ask_user 直接读终端；用 CLIElicitor。
    app = build_agent_app(
        config, tracker=tracker, sink=sink, elicitor=CLIElicitor(), agent_config=agent_config
    )

    if args.resume:
        controller = app.resume_chat(args.resume, on_tool_call=_on_tool_call)
        print(f"[续跑会话] {args.resume}")
        run_chat_repl(controller, streaming=True)
    else:
        task = WritingTask(
            instruction="",  # 首轮由用户在 REPL 里输入
            draft_path=args.input,
            topic_background=args.topic,
        )
        controller = app.open_chat(task, on_tool_call=_on_tool_call)
        print(f"[会话已建] id={controller.session.session_id}"
              f"（下次可用 --resume {controller.session.session_id} 续跑）")
        run_chat_repl(controller, streaming=True)

    print(f"\n用量：{tracker.summary()}")
    if config.tracing_enabled:
        trace_dir = config.trace_dir or os.path.join(config.workspace_dir, "traces")
        print(
            f"追踪已落盘：{trace_dir}（按 trace_id 分文件；"
            f"用 python scripts/trace_view.py <文件> 回放定位）"
        )


if __name__ == "__main__":
    main()
