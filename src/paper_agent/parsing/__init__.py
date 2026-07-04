"""结构化输出解析治理（升级 Req 3）。

`StructuredParser` 收敛「调用 LLM → 解析 JSON → 失败处理」的散落逻辑，
区分测试/Mock 回退路径与生产解析失败路径，杜绝伪造的成功结果。
"""

from __future__ import annotations

from paper_agent.parsing.structured_parser import ParseOutcome, StructuredParser

__all__ = ["ParseOutcome", "StructuredParser"]
