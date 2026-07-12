"""批量运行 Paper Agent 离线评测。

默认使用 Mock provider，仅验证流程与硬约束。要评估真实准确率，请通过 PAPER_LLM、
PAPER_RETRIEVAL 和独立 reviewer 配置真实 provider，并运行 real 案例集。
"""

from __future__ import annotations

import argparse
import os

from paper_agent.config import Config
from paper_agent.evaluation import EvalRunner, discover_cases
from paper_agent.utils.dotenv import load_dotenv


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _parse_args(argv=None):
    parser = argparse.ArgumentParser(description="运行 Paper Agent 离线评测")
    parser.add_argument(
        "--cases",
        default="eval/cases/smoke",
        help="案例 JSON 文件或目录（默认：eval/cases/smoke）",
    )
    parser.add_argument(
        "--output",
        default="eval/results",
        help="评测结果目录（默认：eval/results）",
    )
    parser.add_argument(
        "--case-id",
        action="append",
        default=[],
        help="仅运行指定案例 id；可重复传入",
    )
    parser.add_argument(
        "--llm-provider",
        default=os.environ.get("PAPER_LLM", "mock"),
    )
    parser.add_argument(
        "--llm-model",
        default=os.environ.get("PAPER_LLM_MODEL", ""),
    )
    parser.add_argument(
        "--retrieval-provider",
        default=os.environ.get("PAPER_RETRIEVAL", "mock"),
    )
    parser.add_argument(
        "--iteration-limit",
        type=int,
        default=int(os.environ.get("PAPER_ITER_LIMIT", "2")),
    )
    parser.add_argument(
        "--trace-level",
        choices=("full", "redacted", "off"),
        default=os.environ.get("PAPER_TRACE_LEVEL", "redacted"),
    )
    return parser.parse_args(argv)


def main(argv=None) -> int:
    load_dotenv()
    args = _parse_args(argv)
    config = Config(
        llm_provider=args.llm_provider,
        llm_model=args.llm_model,
        llm_base_url=os.environ.get("PAPER_BASE_URL") or None,
        llm_api_key_env=os.environ.get("PAPER_KEY_ENV") or None,
        retrieval_provider=args.retrieval_provider,
        reviewer_llm_provider=os.environ.get("PAPER_REVIEWER_LLM", ""),
        reviewer_llm_model=os.environ.get("PAPER_REVIEWER_LLM_MODEL", ""),
        reviewer_llm_base_url=os.environ.get("PAPER_REVIEWER_BASE_URL") or None,
        reviewer_llm_api_key_env=os.environ.get("PAPER_REVIEWER_KEY_ENV") or None,
        allow_self_review=_env_bool("PAPER_ALLOW_SELF_REVIEW", False),
        iteration_limit=args.iteration_limit,
        total_token_budget=int(os.environ.get("PAPER_TOKEN_BUDGET", "500000")),
        total_llm_call_budget=int(
            os.environ.get("PAPER_LLM_CALL_BUDGET", "120")
        ),
        llm_completion_token_reserve=int(
            os.environ.get("PAPER_COMPLETION_RESERVE", "4096")
        ),
        wall_clock_deadline_s=float(os.environ.get("PAPER_DEADLINE_S", "1200")),
        adversarial_review_enabled=_env_bool("PAPER_ADVERSARIAL_REVIEW", True),
        citation_faithfulness_enabled=_env_bool("PAPER_FAITHFULNESS", True),
        faithfulness_max_claims=int(
            os.environ.get("PAPER_FAITHFULNESS_MAX_CLAIMS", "12")
        ),
        faithfulness_screen_deadline_s=float(
            os.environ.get("PAPER_FAITHFULNESS_DEADLINE_S", "30")
        ),
        grounding_fulltext_enabled=_env_bool("PAPER_GROUNDING_FULLTEXT", False),
        terminology_extraction_enabled=_env_bool("PAPER_TERMINOLOGY", True),
        language_polish_enabled=_env_bool("PAPER_LANGUAGE_POLISH", True),
        trace_content_level=args.trace_level,
    )

    cases = discover_cases(args.cases)
    if args.case_id:
        selected = set(args.case_id)
        cases = [case for case in cases if case.case_id in selected]
        if not cases:
            print(f"未找到指定案例：{', '.join(args.case_id)}")
            return 2
    result, summary_path = EvalRunner(config, output_root=args.output).run(cases)
    summary = result.to_dict()["summary"]
    print(
        f"评测完成：通过 {summary['passed']}/{summary['executed']}，"
        f"失败 {summary['failed']}，跳过 {summary['skipped']}"
    )
    print(f"报告：{summary_path}")
    for case in result.cases:
        if case.skipped:
            print(f"  SKIP {case.case_id}: {case.skip_reason}")
        elif case.passed:
            print(f"  PASS {case.case_id}")
        else:
            failed = [metric.name for metric in case.metrics if not metric.passed]
            reason = case.error or f"断言失败：{', '.join(failed)}"
            print(f"  FAIL {case.case_id}: {reason}")
    return 0 if result.passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
