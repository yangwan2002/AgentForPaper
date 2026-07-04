r"""导出期的图像资产路径穿越防御（Requirement 4.5 / Property 13）。

导出器在把 ``Figure_Asset`` 嵌入 LaTeX（``\includegraphics``）或 docx
（``add_picture``）之前，必须确认引用的文件确实位于导出目录（含其资产子目录）
之内，绝不写出指向导出目录之外的路径。本模块提供唯一的判定入口
``safe_relative_asset``：

- 把 ``candidate`` 规整为绝对路径（相对路径按相对 ``out_dir`` 解释）；
- 经 ``realpath`` 解析符号链接，防御 ``../`` 穿越、目录外绝对路径与符号链接逃逸；
- 仅当其位于 ``out_dir`` 之内时，返回相对 ``out_dir``、以正斜杠分隔的相对路径
  （适合 LaTeX ``\includegraphics`` 引用），否则返回 ``None``（视为缺资产回退）。

该函数把 ``candidate`` 视为不可信输入：任何格式非法/异常输入都返回 ``None``，
绝不抛出异常。
"""

from __future__ import annotations

import os


def safe_relative_asset(out_dir: str, candidate: str) -> str | None:
    r"""把 ``candidate`` 规整并校验为 ``out_dir`` 之内的相对路径。

    参数
    ----
    out_dir:
        导出目录（绝对或相对均可）。图像资产必须落在此目录（含其任意层级子目录）
        之内才被接受。
    candidate:
        待校验的资产路径。相对路径按相对 ``out_dir`` 解释；绝对路径按其自身解释。

    返回
    ----
    若 ``candidate`` 规整后位于 ``out_dir`` 之内，返回相对 ``out_dir`` 的相对路径
    （正斜杠分隔）；否则（含 ``../`` 穿越、目录外绝对路径、符号链接逃逸、空/非法
    输入）返回 ``None``。任何情况下都不抛异常，也绝不返回指向 ``out_dir`` 之外的路径。
    """
    # 防御式：空 / None / 非字符串输入一律视为缺资产。
    if not out_dir or not candidate:
        return None
    if not isinstance(out_dir, str) or not isinstance(candidate, str):
        return None

    try:
        # out_dir 规整为绝对路径并解析符号链接，作为包含关系判定的基准。
        base = os.path.realpath(os.path.abspath(out_dir))

        # 相对 candidate 按相对 out_dir 解释；绝对 candidate 保持自身。
        if os.path.isabs(candidate):
            target = os.path.realpath(candidate)
        else:
            target = os.path.realpath(os.path.join(base, candidate))

        # 用 commonpath 判定包含关系：仅当二者最长公共路径等于 base 时，
        # target 才真正位于 base 之内。commonpath 在跨盘符（Windows）或
        # 混合绝对/相对时抛 ValueError，一并视为目录外。
        if base == target:
            # candidate 规整后恰为 out_dir 本身，不是其内部文件。
            return None
        if os.path.commonpath([base, target]) != base:
            return None

        rel = os.path.relpath(target, base)
    except (ValueError, OSError):
        # 跨盘符、非法路径、系统调用错误等——一律视为缺资产回退。
        return None

    # 二次防御：规整后的相对路径不得逃逸（不以 os.pardir 起头，也不是绝对路径）。
    if os.path.isabs(rel):
        return None
    first = rel.replace("\\", "/").split("/", 1)[0]
    if first == os.pardir:
        return None

    # 统一使用正斜杠分隔，适合 LaTeX \includegraphics 与跨平台引用。
    return rel.replace("\\", "/")


__all__ = ["safe_relative_asset"]
