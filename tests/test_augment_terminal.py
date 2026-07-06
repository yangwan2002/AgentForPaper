"""augment_document 选路引导测试（inplace-augment-sections · Task 4）。

验证 augment_document 属于交付类工具（产出文件即收尾），且系统提示含保格式增补红线。
"""

from __future__ import annotations

from paper_agent.agent_platform import task_agent as ta


def test_augment_document_is_terminal_tool():
    assert "augment_document" in ta._TERMINAL_TOOLS


def test_system_prompt_has_augment_redline():
    prompt = ta._SYSTEM_PROMPT
    assert "augment_document" in prompt
    assert "保格式增补" in prompt
