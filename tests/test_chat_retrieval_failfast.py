"""chat.py 生产入口的检索 fail-fast 测试（P0-3）。

mock 检索会返回假文献、误导「加文献」类任务，故生产入口默认拒绝启动，
除非显式 PAPER_ALLOW_MOCK_RETRIEVAL=1。真实检索直接放行。
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

from paper_agent.config import Config


def _load_chat_module():
    root = Path(__file__).resolve().parents[1]
    path = root / "scripts" / "chat.py"
    spec = importlib.util.spec_from_file_location("chat_cli_under_test", str(path))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_mock_retrieval_fails_fast(monkeypatch):
    monkeypatch.delenv("PAPER_ALLOW_MOCK_RETRIEVAL", raising=False)
    chat = _load_chat_module()
    config = Config(llm_provider="mock", retrieval_provider="mock")
    with pytest.raises(SystemExit) as exc:
        chat._check_retrieval_or_exit(config)
    assert exc.value.code == 2


def test_mock_retrieval_allowed_when_flag_set(monkeypatch):
    monkeypatch.setenv("PAPER_ALLOW_MOCK_RETRIEVAL", "1")
    chat = _load_chat_module()
    config = Config(llm_provider="mock", retrieval_provider="mock")
    # 显式允许 → 不退出（仅告警）。
    chat._check_retrieval_or_exit(config)


def test_real_retrieval_passes(monkeypatch):
    monkeypatch.delenv("PAPER_ALLOW_MOCK_RETRIEVAL", raising=False)
    chat = _load_chat_module()
    config = Config(llm_provider="mock", retrieval_provider="openalex")
    # 真实检索直接放行。
    chat._check_retrieval_or_exit(config)
