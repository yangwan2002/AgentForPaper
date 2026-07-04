r"""导出期的正文内引用转换（#2 修复）。

此前三个导出器都把模型写的行内 ``[id]`` 标注当字面文本留在正文里，只在章节
末尾另起一行集中列出引用——导致导出论文正文里残留裸 ``[arxiv:1706.03762]``
字符串、引用与引用位置脱节。本模块提供统一的行内替换：

- 把正文里 ``[id]``（id 在映射中）就地替换为导出格式对应的引用记号
  （markdown/docx 的 ``[n]``、LaTeX 的 ``\cite{key}``）；
- 返回已内联渲染的 id 集合，供导出器对**未在正文出现**的已记录引用补一条
  章节末尾的回退引用（避免漏引），同时不与内联重复。
"""

from __future__ import annotations

import re

# 正文里形如 [id] 的引用标注；id 限 ASCII 标识符字符（含冒号/点/连字符/下划线）。
_TEXT_CITATION = re.compile(r"\[([A-Za-z0-9_.:\-]+)\]")


def render_inline_citations(
    content: str, id_to_token: dict[str, str]
) -> tuple[str, set[str]]:
    """把正文里 ``[id]``（id 在 ``id_to_token`` 中）替换为对应记号。

    返回 ``(新正文, 已内联渲染的 id 集合)``。不在映射中的 ``[..]`` 原样保留
    （如 ``[表格 第1页 #1]`` 含空格/CJK 也不匹配，自动跳过）。
    """
    rendered: set[str] = set()

    def _repl(m: re.Match) -> str:
        cid = m.group(1)
        token = id_to_token.get(cid)
        if token is None:
            return m.group(0)
        rendered.add(cid)
        return token

    new_content = _TEXT_CITATION.sub(_repl, content or "")
    return new_content, rendered


__all__ = ["render_inline_citations"]
