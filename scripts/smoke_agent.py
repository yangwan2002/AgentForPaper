"""真实 LLM 冒烟测试：验证 agent 平台在真实模型上的工具编排与护栏。

不进 CI（会产生真实 API 调用）。用法：
    python scripts/smoke_agent.py            # 场景1：改写实验章节（局部隔离）
    python scripts/smoke_agent.py refs       # 场景2：增补参考文献（需 PAPER_RETRIEVAL=openalex）

依赖 .env 中的 PAPER_LLM/PAPER_BASE_URL/PAPER_LLM_MODEL/PAPER_KEY_ENV。
"""

from __future__ import annotations

import os
import sys
import tempfile

# Windows 控制台默认 GBK，打印 emoji/部分字符会崩；切到 UTF-8（容错替换）。
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:  # noqa: BLE001 - 老环境不支持 reconfigure 时忽略
    pass

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from paper_agent.agent_platform.app import build_agent_app  # noqa: E402
from paper_agent.agent_platform.models import WritingTask  # noqa: E402
from paper_agent.config import Config  # noqa: E402
from paper_agent.observability.console import ConsoleReporter  # noqa: E402
from paper_agent.observability.usage import UsageTracker  # noqa: E402
from paper_agent.utils.dotenv import load_dotenv  # noqa: E402
from paper_agent.workspace.models import (  # noqa: E402
    InputMode,
    OutlineNode,
    OutputFormat,
    PaperWorkspace,
    SectionDraft,
)
from paper_agent.workspace.repository import WorkspaceRepository  # noqa: E402
from paper_agent.workspace.store import JsonFileStore  # noqa: E402


def _config(workspace_dir: str) -> Config:
    return Config(
        llm_provider=os.environ.get("PAPER_LLM", "anthropic"),
        llm_model=os.environ.get("PAPER_LLM_MODEL", ""),
        llm_base_url=os.environ.get("PAPER_BASE_URL") or None,
        llm_api_key_env=os.environ.get("PAPER_KEY_ENV") or None,
        retrieval_provider=os.environ.get("PAPER_RETRIEVAL", "mock"),
        workspace_dir=workspace_dir,
        default_output_format=OutputFormat.MARKDOWN,
        allow_self_review=os.environ.get("PAPER_ALLOW_SELF_REVIEW", "0") == "1",
        wall_clock_deadline_s=float(os.environ.get("PAPER_DEADLINE_S", "600")),
        total_token_budget=int(os.environ.get("PAPER_TOKEN_BUDGET", "0")),
    )


def _seed_workspace(store: JsonFileStore) -> PaperWorkspace:
    repo = WorkspaceRepository(store)
    ws = PaperWorkspace(
        workspace_id="smoke01",
        input_mode=InputMode.DRAFT_REVISION,
        output_format=OutputFormat.MARKDOWN,
    )
    ws.outline = [
        OutlineNode(section_id="introduction", title="引言", order=0),
        OutlineNode(section_id="experiments", title="实验", order=1),
    ]
    ws.section_drafts = {
        "introduction": SectionDraft(
            section_id="introduction", title="引言",
            content="本文研究图神经网络在推荐系统中的应用。近年来该方向受到广泛关注。",
        ),
        "experiments": SectionDraft(
            section_id="experiments", title="实验",
            content=(
                "在本次的实验部分当中，我们其实进行了非常多的各种各样的实验，"
                "这些实验都是为了能够去验证我们所提出来的这个方法到底是不是真的有效，"
                "我们用了很多的数据集，然后也对比了很多的基线方法，结果表明我们的方法是好的。"
            ),
        ),
    }
    repo.create(ws)
    return ws


def _print_transcript(session_transcript):
    print("\n=== 工具调用轨迹 ===")
    for i, e in enumerate(session_transcript, 1):
        if e.get("kind") == "tool_call":
            print(f"  {i}. 调用工具 {e.get('name')}  args={e.get('args')}")
        else:
            print(f"  {i}. [{e.get('kind')}] {dict((k, v) for k, v in e.items() if k != 'kind')}")


def scenario_rewrite(app, store, ws):
    print("\n########## 场景1：改写实验章节（应只动实验、不碰引言）##########")
    before_intro = ws.section_drafts["introduction"].content
    before_exp = ws.section_drafts["experiments"].content
    print(f"\n[改写前] 实验章节：\n{before_exp}\n")

    task = WritingTask(
        instruction="请把实验章节的叙述改得更简洁、更学术，去掉口语化的冗词。不要改动引言等其他章节。",
        workspace_id=ws.workspace_id,
    )
    result = app.run_task(task)

    reloaded = WorkspaceRepository(store).load(ws.workspace_id)
    print(f"[改写后] 实验章节：\n{reloaded.section_drafts['experiments'].content}\n")
    print(f"[引言是否被动] {'未变（正确）' if reloaded.section_drafts['introduction'].content == before_intro else '被改动（异常）'}")
    print(f"\n[最终答复] {result.summary}")
    print(f"[护栏] {result.guardrail_report}  [上限] {result.bound_hit}")


def main():
    load_dotenv()
    scenario = sys.argv[1] if len(sys.argv) > 1 else "rewrite"
    workspace_dir = os.path.join(tempfile.gettempdir(), "paper_agent_smoke")
    os.makedirs(workspace_dir, exist_ok=True)

    config = _config(workspace_dir)
    store = JsonFileStore(workspace_dir)
    ws = _seed_workspace(store)

    reporter = ConsoleReporter(show_thinking=False, show_llm=False)
    tracker = UsageTracker()
    app = build_agent_app(config, store=store, sink=reporter, tracker=tracker)

    # 用会话对象观察 transcript：run_task 内部持久化了 transcript，这里从会话读。
    session = app._intake.start(ws_task := WritingTask(
        instruction="请把实验章节的叙述改得更简洁、更学术，去掉口语化冗词，不要改动引言。",
        workspace_id=ws.workspace_id,
    ))
    result = app._run_session(session)

    reloaded = WorkspaceRepository(store).load(ws.workspace_id)
    print("\n[改写后] 实验章节：\n" + reloaded.section_drafts["experiments"].content)
    print("\n[引言是否被动] " + (
        "未变（正确）" if reloaded.section_drafts["introduction"].content
        == ws.section_drafts["introduction"].content else "被改动（异常）"))
    _print_transcript(session.transcript)
    print(f"\n[最终答复] {result.summary}")
    print(f"[护栏] {result.guardrail_report}   [上限] {result.bound_hit}")
    print(f"[用量] {tracker.summary()}")


if __name__ == "__main__":
    main()
