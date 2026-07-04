"""用真实 LLM 跑一篇论文的 CLI（不进 CI，避免产生 API 费用）。

一条命令即可用——给一个**初稿文件**或一个**主题**，系统据文件类型自动选择处理方式：

    python scripts/run_real.py my_draft.tex     # LaTeX 初稿 → 保结构原地润色
    python scripts/run_real.py my_draft.docx    # Word 初稿  → 保结构原地润色
    python scripts/run_real.py my_draft.md       # md/txt/pdf → 完整重渲染管线
    python scripts/run_real.py "我的论文主题"     # 无初稿      → 从零生成

默认**交互**：系统拿不准时（缺章节/缺引用/缺数据/研究描述）会一次性问你几个问题。
加 --yes 则全程非交互、取最保守默认。

参数：
    <input>            初稿文件路径 或 论文主题（二选一，自动识别）
    --yes              非交互：跳过所有澄清/访谈，取最保守默认
    --resume <id>      续跑指定工作区
    --rebuild          对 .tex/.docx 强制走完整重渲染（**会丢原排版**）
  进阶（可选）：
    --artifact <dir>   真实研究内容目录（artifact.yaml + experiments/*.csv）
    --profile <file>   论文档案（目标期刊/风格/引用规范）
    --styles <dir>     会议样式文件目录（.sty/.cls）

可选环境变量（也可写进 .env）：
    PAPER_LLM / PAPER_LLM_MODEL / PAPER_BASE_URL / PAPER_KEY_ENV
    PAPER_REVIEWER_LLM / ...（reviewer 独立模型，打破自评）
    PAPER_RETRIEVAL（api|mock|openalex|arxiv）  PAPER_OUTPUT（覆盖「输出=输入格式」默认）
    PAPER_VENUE  PAPER_STYLES  PAPER_ITER_LIMIT  PAPER_DEADLINE_S  PAPER_TOKEN_BUDGET
    PAPER_ALLOW_SELF_REVIEW  PAPER_SHOW_THINKING  PAPER_SHOW_LLM
"""

from __future__ import annotations

import argparse
import os
import shutil
import sys

# 允许直接运行而无需安装包。
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from paper_agent.app import build_from_config  # noqa: E402
from paper_agent.config import Config  # noqa: E402
from paper_agent.entry import (  # noqa: E402
    Engine,
    decide_engine,
    default_output_format,
    looks_like_draft,
)
from paper_agent.ingestion import DocumentLoadError, load_document  # noqa: E402
from paper_agent.ingestion.artifact_loader import (  # noqa: E402
    ArtifactLoadError,
    load_artifact,
)
from paper_agent.observability.console import ConsoleReporter  # noqa: E402
from paper_agent.observability.usage import UsageTracker  # noqa: E402
from paper_agent.orchestrator import PaperRequest  # noqa: E402
from paper_agent.profile import load_profile  # noqa: E402
from paper_agent.utils.dotenv import load_dotenv  # noqa: E402
from paper_agent.workspace.models import OutputFormat  # noqa: E402


# ------------------------------------------------------------------ #
# LLM 装配
# ------------------------------------------------------------------ #

def _build_polish_llm():
    """为原地润色构造 LLM 栈（Resilient 包一层），返回 (llm, is_mock)。"""
    from paper_agent.providers.factory import build_llm_provider
    from paper_agent.providers.llm.mock import MockLLMProvider
    from paper_agent.providers.llm.resilient import ResilientLLMProvider
    from paper_agent.workspace.models import RetryPolicy

    config = Config(
        llm_provider=os.environ.get("PAPER_LLM", "openai"),
        llm_model=os.environ.get("PAPER_LLM_MODEL", ""),
        llm_base_url=os.environ.get("PAPER_BASE_URL") or None,
        llm_api_key_env=os.environ.get("PAPER_KEY_ENV") or None,
    )
    base_llm = build_llm_provider(config)
    is_mock = isinstance(base_llm, MockLLMProvider)
    return ResilientLLMProvider(base_llm, RetryPolicy(), None), is_mock


# ------------------------------------------------------------------ #
# 保结构原地润色（.tex / .docx）
# ------------------------------------------------------------------ #

def _extract_latex_titles(source: str) -> list[tuple[str, str]]:
    """从 LaTeX 源提取 ``\\section{}``/``\\subsection{}`` 标题（供缺口扫描）。"""
    import re as _re

    return [
        (m.group(1), m.group(1))
        for m in _re.finditer(r"\\(?:sub){0,2}section\*?\{([^{}]*)\}", source)
    ]


def _extract_docx_text_and_titles(path: str) -> tuple[str, list[tuple[str, str]]]:
    """用 python-docx 提取 DOCX 正文文本 + 标题段落（供缺口扫描）。

    不可用 python-docx 时返回空（in-place 模式本就依赖该库，缺则后续会报错）。
    """
    try:
        import docx  # noqa: WPS433
    except ImportError:
        return "", []
    try:
        doc = docx.Document(path)
    except Exception:  # noqa: BLE001 - 文件损坏等：缺口扫描降级为空
        return "", []
    titles: list[tuple[str, str]] = []
    parts: list[str] = []
    for para in doc.paragraphs:
        text = (para.text or "").strip()
        if text:
            parts.append(text)
        try:
            style_name = (para.style.name or "") if para.style else ""
        except Exception:  # noqa: BLE001
            style_name = ""
        if style_name and any(
            s in style_name.lower() for s in ("heading", "title", "标题")
        ):
            titles.append((text, text))
    return "\n".join(parts), titles


def _clarify_inplace(
    draft_path: str, ext: str, interactive: bool
) -> bool:
    """In-place 模式前的缺口澄清：检测 in-place 处理不了的缺口，问用户是否改走完整管线。

    in-place（LaTeX/DOCX 原地润色）只能改语言，无法补章节/补引用/核验数字。若检测到
    这些缺口，问用户「继续原地润色」还是「改走完整管线（会丢原排版）」。

    Returns:
        ``True`` 表示用户选择改走完整管线（调用方应 reroute 到 ``_run_pipeline``）；
        ``False`` 表示继续 in-place（无缺口，或用户选择继续）。

    非交互（``interactive=False``）下：``AutoElicitor`` 取默认「继续原地润色」，
    行为确定、不阻塞——与既有 in-place 行为一致。
    """
    from paper_agent.clarification import build_inplace_reroute_question
    from paper_agent.draft_analyzer import analyze_text
    from paper_agent.elicitation import AutoElicitor, CLIElicitor

    # 读源 + 提取标题。
    if ext in (".tex", ".latex"):
        if not os.path.isfile(draft_path):
            raise SystemExit(f"文件不存在：{draft_path}")
        with open(draft_path, "r", encoding="utf-8-sig") as fh:
            source = fh.read()
        titles = _extract_latex_titles(source)
    elif ext == ".docx":
        source, titles = _extract_docx_text_and_titles(draft_path)
    else:
        return False  # 其他格式不走 in-place

    gaps = analyze_text(source, titles=titles, has_artifact=False)
    question = build_inplace_reroute_question(gaps)
    if question is None:
        return False  # 无 actionable 缺口 → 直接 in-place

    elicitor = CLIElicitor() if interactive else AutoElicitor()
    ans = elicitor.ask(question)
    return ans.startswith("改走完整管线")


def _run_latex_inplace(draft_path: str) -> None:
    """LaTeX 原地润色：读 .tex → 保结构只润散文 → 写 <名>.polished.tex。"""
    if not os.path.isfile(draft_path):
        raise SystemExit(f"文件不存在：{draft_path}")
    with open(draft_path, "r", encoding="utf-8-sig") as fh:
        source = fh.read()

    from paper_agent.latex_inplace import InplaceLatexPolisher

    llm, is_mock = _build_polish_llm()
    print(f"[LaTeX 原地润色] 读入：{draft_path}（{len(source)} 字符）")
    if is_mock:
        print("[警告] 当前为 Mock provider，原地润色为 no-op（输出逐字节等于输入）；"
              "请配置真实 PAPER_LLM 才会实际润色。")

    result = InplaceLatexPolisher(llm, is_mock=is_mock).polish(source)
    stem = os.path.splitext(os.path.basename(draft_path))[0]
    os.makedirs("output", exist_ok=True)
    out_path = os.path.join("output", f"{stem}.polished.tex")
    with open(out_path, "w", encoding="utf-8") as fh:
        fh.write(result.source)
    for note in result.notes:
        print(f"  {note}")
    print(f"产出文件：{out_path}")


def _run_docx_inplace(draft_path: str) -> None:
    """DOCX 原地润色：保 OOXML 结构 → 只改正文散文 → 写 <名>.polished.docx。"""
    if not os.path.isfile(draft_path):
        raise SystemExit(f"文件不存在：{draft_path}")

    from paper_agent.docx_inplace import InplaceDocxPolisher

    llm, is_mock = _build_polish_llm()
    print(f"[DOCX 原地润色] 读入：{draft_path}")
    if is_mock:
        print("[警告] 当前为 Mock provider，DOCX 原地润色为 no-op（复制原文）；"
              "请配置真实 PAPER_LLM 才会实际润色。")

    stem = os.path.splitext(os.path.basename(draft_path))[0]
    os.makedirs("output", exist_ok=True)
    out_path = os.path.join("output", f"{stem}.polished.docx")
    result = InplaceDocxPolisher(llm, is_mock=is_mock).polish(draft_path, out_path)
    for note in result.notes:
        print(f"  {note}")
    print(f"产出文件：{result.out_path}")


# ------------------------------------------------------------------ #
# 完整管线（.md/.txt/.pdf 初稿 或 主题）
# ------------------------------------------------------------------ #

def _read_draft(path: str) -> str:
    base = os.path.splitext(os.path.basename(path))[0]
    asset_dir = os.path.join("output", f"{base}_assets")
    try:
        return load_document(path, asset_dir=asset_dir)
    except DocumentLoadError as exc:
        raise SystemExit(str(exc))


def _build_pipeline_config(args, draft_path: str | None, interactive: bool) -> Config:
    """据环境变量 + 初稿类型构造完整管线的 Config。输出格式默认=输入格式。"""
    out_env = os.environ.get("PAPER_OUTPUT")
    output_format = (
        OutputFormat(out_env) if out_env else default_output_format(draft_path)
    )
    return Config(
        llm_provider=os.environ.get("PAPER_LLM", "openai"),
        llm_model=os.environ.get("PAPER_LLM_MODEL", ""),
        llm_base_url=os.environ.get("PAPER_BASE_URL") or None,
        llm_api_key_env=os.environ.get("PAPER_KEY_ENV") or None,
        retrieval_provider=os.environ.get("PAPER_RETRIEVAL", "api"),
        default_output_format=output_format,
        workspace_dir=os.environ.get("PAPER_WORKSPACE_DIR", "output"),
        iteration_limit=int(os.environ.get("PAPER_ITER_LIMIT", "3")),
        venue_id=os.environ.get("PAPER_VENUE", "default"),
        styles_dir=args.styles or os.environ.get("PAPER_STYLES") or None,
        # 交互模式启用 LLM 动态澄清；--yes（非交互）关闭。
        llm_clarifying_questions_enabled=interactive,
        wall_clock_deadline_s=float(os.environ.get("PAPER_DEADLINE_S", "1800")),
        total_token_budget=int(os.environ.get("PAPER_TOKEN_BUDGET", "2000000")),
        enable_pdflatex_check=(shutil.which("pdflatex") is not None),
        reviewer_llm_provider=os.environ.get("PAPER_REVIEWER_LLM", ""),
        reviewer_llm_model=os.environ.get("PAPER_REVIEWER_LLM_MODEL", ""),
        reviewer_llm_base_url=os.environ.get("PAPER_REVIEWER_BASE_URL") or None,
        reviewer_llm_api_key_env=os.environ.get("PAPER_REVIEWER_KEY_ENV") or None,
        allow_self_review=os.environ.get("PAPER_ALLOW_SELF_REVIEW", "0") == "1",
    )


def _run_pipeline(
    args, draft_path: str | None, topic: str | None, interactive: bool
) -> None:
    from paper_agent.elicitation import AutoElicitor, CLIElicitor

    config = _build_pipeline_config(args, draft_path, interactive)
    reporter = ConsoleReporter(
        show_thinking=os.environ.get("PAPER_SHOW_THINKING", "1") != "0",
        show_llm=os.environ.get("PAPER_SHOW_LLM", "1") != "0",
    )
    tracker = UsageTracker()
    elicitor = CLIElicitor() if interactive else AutoElicitor()
    orch = build_from_config(config, sink=reporter, tracker=tracker, elicitor=elicitor)

    if args.resume:
        print(f"[续跑模式] 工作区：{args.resume}")
        result = orch.run(resume_id=args.resume)
    else:
        profile = load_profile(args.profile).to_dict() if args.profile else {}
        if profile:
            print(f"[论文档案] 已加载：{args.profile}")
        artifact = None
        if args.artifact:
            try:
                artifact = load_artifact(args.artifact)
            except ArtifactLoadError as exc:
                raise SystemExit(f"加载 artifact 失败：{exc}")
            print(
                f"[真实研究内容] 已加载：{args.artifact}"
                f"（{len(artifact.experiments)} 个实验，"
                f"{len(artifact.contributions)} 条贡献）"
            )
        if draft_path:
            draft = _read_draft(draft_path)
            # 记录输入路径到 profile，供 orchestrator 澄清阶段检测「输出格式与输入
            # 不一致」缺口（.tex 输入却选 docx 输出 → 提示会丢 LaTeX 结构）。
            profile["input_path"] = os.path.abspath(draft_path)
            request = PaperRequest(draft=draft, profile=profile, artifact=artifact)
            print(f"[草稿修订模式] 读入初稿：{draft_path}（{len(draft)} 字符）")
        else:
            request = PaperRequest(
                topic_background=topic, profile=profile, artifact=artifact
            )
            print(f"[从零生成模式] 主题：{topic}")
            # 交互模式下，若无 artifact 则访谈采集领域/问题/方法（反 hallucination）。
            if artifact is None and interactive:
                from paper_agent.ingestion.interactive_intake import run_intake

                intake_artifact = run_intake(elicitor)
                if intake_artifact is not None:
                    request.artifact = intake_artifact
                    artifact = intake_artifact
                    print(
                        f"[研究描述] 已采集：{intake_artifact.research_question}"
                        f"（方法：{intake_artifact.method.overview[:60]}…）"
                    )
            if artifact is None:
                print(
                    "[警告] 无 artifact 且未完成研究描述：产出为「LLM 推断版」，"
                    "方法/实验/数字可能与真实研究不符（加交互或 --artifact 可改善）。"
                )
        result = orch.run(request)

    print(f"\n终止原因：{result.terminated_reason}")
    print(f"未达标维度：{[d.value for d in result.unmet_dimensions]}")
    print(f"可投递：{'是' if result.submittable else '否'}")
    if result.submittability_notes:
        print("可投递性说明：")
        for line in result.submittability_notes:
            print(f"  {line}")
    print(f"用量：{tracker.summary()}")
    if result.export:
        print("产出文件：")
        for f in result.export.files:
            print(f"  - {f}")


# ------------------------------------------------------------------ #
# CLI
# ------------------------------------------------------------------ #

def _parse_args(argv=None):
    parser = argparse.ArgumentParser(
        description="学术论文写作 agent —— 给一个初稿文件或主题即可"
    )
    parser.add_argument(
        "input",
        nargs="?",
        help="初稿文件路径（.tex/.docx/.md/.txt/.pdf）或论文主题（自动识别）",
    )
    parser.add_argument("--yes", action="store_true", help="非交互：跳过所有澄清，取最保守默认")
    parser.add_argument("--resume", help="续跑指定工作区 id")
    parser.add_argument(
        "--task",
        help="自然语言任务描述（启用智能体平台模式）。可与初稿文件/主题同时给出，"
        "如 --task \"帮我改写实验章节并加5篇近年文献\" my_draft.tex",
    )
    parser.add_argument(
        "--rebuild",
        action="store_true",
        help="对 .tex/.docx 强制走完整重渲染管线（会丢原排版），而非默认保结构原地润色",
    )
    parser.add_argument("--draft", help="[进阶] 显式指定初稿文件（等价于位置参数给文件）")
    parser.add_argument("--artifact", help="[进阶] 真实研究内容目录（artifact.yaml + CSV）")
    parser.add_argument("--profile", help="[进阶] 论文档案 steering 文件")
    parser.add_argument("--styles", help="[进阶] 会议样式文件目录（.sty/.cls）")
    return parser.parse_args(argv)


def _classify_input(args) -> tuple[str | None, str | None]:
    """把输入解析为 (draft_path, topic)。--draft 显式优先；否则据位置参数是否像文件判定。"""
    if args.draft:
        return args.draft, None
    raw = args.input
    if raw and looks_like_draft(raw):
        return raw, None
    return None, raw


def _run_agent_platform(args, draft_path, topic, interactive) -> None:
    """智能体平台模式：自然语言任务驱动，自主编排工具完成（Req 1/2）。"""
    from paper_agent.agent_platform.app import build_agent_app
    from paper_agent.agent_platform.models import WritingTask
    from paper_agent.elicitation import AutoElicitor, CLIElicitor

    config = _build_pipeline_config(args, draft_path, interactive)
    reporter = ConsoleReporter(
        show_thinking=os.environ.get("PAPER_SHOW_THINKING", "1") != "0",
        show_llm=os.environ.get("PAPER_SHOW_LLM", "1") != "0",
    )
    tracker = UsageTracker()
    elicitor = CLIElicitor() if interactive else AutoElicitor()
    app = build_agent_app(config, sink=reporter, tracker=tracker, elicitor=elicitor)

    if args.resume:
        print(f"[智能体·续跑] 会话：{args.resume}")
        result = app.resume(args.resume)
    else:
        profile = load_profile(args.profile).to_dict() if args.profile else {}
        artifact = None
        if args.artifact:
            try:
                artifact = load_artifact(args.artifact)
            except ArtifactLoadError as exc:
                raise SystemExit(f"加载 artifact 失败：{exc}")
        task = WritingTask(
            instruction=args.task or "",
            draft_path=draft_path,
            topic_background=topic,
            artifact=artifact,
            profile=profile,
        )
        print(f"[智能体模式] 任务：{args.task}")
        result = app.run_task(task)

    print(f"\n结果：{result.summary}")
    if result.bound_hit:
        print(f"触达上限：{result.bound_hit}")
    print(f"护栏：{result.guardrail_report}")
    print(f"用量：{tracker.summary()}")
    if result.export_files:
        print("产出文件：")
        for f in result.export_files:
            print(f"  - {f}")


def main(argv=None) -> None:
    load_dotenv()
    args = _parse_args(argv)

    if not args.resume and not args.input and not args.draft and not args.task:
        raise SystemExit("请给一个初稿文件或论文主题（或用 --task 下达任务 / --resume 续跑）。")

    interactive = not args.yes
    draft_path, topic = _classify_input(args)

    # 智能体平台模式：给了 --task 即启用自然语言任务驱动（Req 1）。
    if args.task:
        _run_agent_platform(args, draft_path, topic, interactive)
        return

    # 续跑：直接走管线续跑逻辑。
    if args.resume:
        _run_pipeline(args, None, None, interactive)
        return

    # 文件类型自动路由：.tex/.docx 默认保结构原地润色（--rebuild 强制重渲染）。
    engine = decide_engine(draft_path, rebuild=args.rebuild)
    if engine is Engine.LATEX_INPLACE:
        print("[自动路由] LaTeX 初稿 → 保结构原地润色（--rebuild 可强制重渲染）。")
        # 检测 in-place 处理不了的缺口（缺章节/缺引用/数字无数据），问用户是否 reroute。
        if _clarify_inplace(draft_path, ".tex", interactive):
            print("[澄清] 用户选择改走完整管线，reroute 到重渲染管线（会丢原排版）。")
            _run_pipeline(args, draft_path, None, interactive)
        else:
            _run_latex_inplace(draft_path)
        return
    if engine is Engine.DOCX_INPLACE:
        print("[自动路由] Word 初稿 → 保结构原地润色（--rebuild 可强制重渲染）。")
        if _clarify_inplace(draft_path, ".docx", interactive):
            print("[澄清] 用户选择改走完整管线，reroute 到重渲染管线（会丢原排版）。")
            _run_pipeline(args, draft_path, None, interactive)
        else:
            _run_docx_inplace(draft_path)
        return

    _run_pipeline(args, draft_path, topic, interactive)


if __name__ == "__main__":
    main()
