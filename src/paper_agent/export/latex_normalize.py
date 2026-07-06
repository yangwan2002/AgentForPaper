"""LaTeX → pandoc 转换前的**确定性预规整**（正确性核心，绝不交 LLM 现做）。

问题背景：某些 LaTeX 表格单元格用 ``\\shortstack{甲\\\\乙}`` / ``\\makecell{甲\\\\乙}``
做「单元格内换行」，其内部的 ``\\\\`` 与表格**行分隔符** ``\\\\`` 撞车。pandoc 的表格解析
器无法区分二者，遇到就**放弃整张表**、把 LaTeX 源码原样吐成纯文本段落（用户看到的
「@lcccc@ 甲 & 乙 & …」正是这种「转换失败回吐原文」，不是乱码）。尤其当
``\\shortstack{甲\\\\乙}`` 嵌在 ``\\multirow{n}{*}{...}`` 里时必然触发。

修法（已用 pandoc 3.x 实测验证）：转换前把 ``\\shortstack``/``\\makecell`` 的**壳去掉、
内部 ``\\\\`` 折成空格**——单元格内的多行堆叠退化为单行文本。这是刻意的保真取舍：宁可
让「共视比例/区间」这类堆叠表头变成「共视比例 区间」单行，也远好过整张表崩成纯文本。

本模块**只做纯文本→纯文本**的确定性变换，不依赖 pandoc、不碰用户原文件（调用方在
副本上应用）。
"""

from __future__ import annotations

import re

# 行内换行标记：``\\`` 可带可选的行距参数（如 ``\\[2pt]``）。折成一个空格。
_ROW_BREAK = re.compile(r"\\\\(?:\s*\[[^\]]*\])?")

# 会导致 pandoc 表格解析崩溃的「单元格内多行」命令（单参数、可带 [pos] 可选参数）。
_STACK_COMMANDS = ("shortstack", "makecell")


def _flatten_command(text: str, cmd: str) -> tuple[str, int]:
    """把所有 ``\\cmd[可选]{内容}`` 替换为「内容」，且内容里的 ``\\\\`` 折成空格。

    用平衡花括号扫描（非正则），正确处理内容里的嵌套 ``{}``；对嵌套的同名命令递归处理。
    返回 (新文本, 替换次数)。
    """
    token = "\\" + cmd
    out: list[str] = []
    i, n, count = 0, len(text), 0
    while i < n:
        j = text.find(token, i)
        if j == -1:
            out.append(text[i:])
            break
        k = j + len(token)
        # 后面紧跟字母 → 是别的命令（如 \shortstackfoo），原样跳过。
        if k < n and text[k].isalpha():
            out.append(text[i:k])
            i = k
            continue
        out.append(text[i:j])

        p = k
        while p < n and text[p] in " \t":
            p += 1
        # 跳过可选参数 [pos]。
        if p < n and text[p] == "[":
            depth, p = 1, p + 1
            while p < n and depth:
                if text[p] == "[":
                    depth += 1
                elif text[p] == "]":
                    depth -= 1
                p += 1
        while p < n and text[p] in " \t":
            p += 1
        # 必须跟 {内容}，否则不是我们要处理的形态，原样保留 token。
        if p < n and text[p] == "{":
            depth, q, start = 1, p + 1, p + 1
            while q < n and depth:
                c = text[q]
                if c == "{":
                    depth += 1
                elif c == "}":
                    depth -= 1
                    if depth == 0:
                        break
                q += 1
            content = text[start:q]
            content, _ = _flatten_command(content, cmd)   # 递归处理嵌套同名命令
            content = _ROW_BREAK.sub(" ", content)
            out.append(re.sub(r"\s+", " ", content).strip())
            count += 1
            i = q + 1
        else:
            out.append(token)
            i = k
    return "".join(out), count


def normalize_latex_for_pandoc(text: str) -> tuple[str, list[str]]:
    """对 LaTeX 源做转换前预规整，返回 (新文本, 人可读说明片段)。

    目前处理：折平 ``\\shortstack`` / ``\\makecell`` 单元格内换行（防表格崩成纯文本）。
    无改动时返回原文本与空说明。
    """
    notes: list[str] = []
    total = 0
    for cmd in _STACK_COMMANDS:
        text, n = _flatten_command(text, cmd)
        total += n
    if total:
        notes.append(
            f"（转换前已折平 {total} 处 \\shortstack/\\makecell 单元格内换行，"
            "避免表格被 pandoc 误当纯文本吐出。）"
        )
    return text, notes


__all__ = ["normalize_latex_for_pandoc"]
