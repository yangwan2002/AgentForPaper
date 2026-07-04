"""零依赖 .env 加载器测试。"""

from __future__ import annotations

import os

from paper_agent.utils.dotenv import load_dotenv


def test_loads_keys_and_strips_quotes(tmp_path, monkeypatch):
    env = tmp_path / ".env"
    env.write_text(
        "# 注释\n"
        "\n"
        "export FOO=bar\n"
        'BAZ="quoted value"\n'
        "EMPTY=\n",
        encoding="utf-8",
    )
    monkeypatch.delenv("FOO", raising=False)
    monkeypatch.delenv("BAZ", raising=False)

    assert load_dotenv(str(env)) is True
    assert os.environ["FOO"] == "bar"
    assert os.environ["BAZ"] == "quoted value"


def test_does_not_override_existing_by_default(tmp_path, monkeypatch):
    env = tmp_path / ".env"
    env.write_text("FOO=from_file\n", encoding="utf-8")
    monkeypatch.setenv("FOO", "from_env")

    load_dotenv(str(env))
    assert os.environ["FOO"] == "from_env"  # 已存在的环境变量优先

    load_dotenv(str(env), override=True)
    assert os.environ["FOO"] == "from_file"


def test_missing_file_returns_false(tmp_path):
    assert load_dotenv(str(tmp_path / "nope.env")) is False
