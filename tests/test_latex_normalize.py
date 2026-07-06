"""LaTeX 转换前预规整：折平 \\shortstack / \\makecell 单元格内换行。"""

from __future__ import annotations

from paper_agent.export.latex_normalize import normalize_latex_for_pandoc


def test_flatten_shortstack_internal_rowbreak():
    text = r"\shortstack{共视比例\\区间}"
    out, notes = normalize_latex_for_pandoc(text)
    assert out == "共视比例 区间"
    assert notes  # 有改动应给说明


def test_shortstack_nested_in_multirow_is_flattened_but_multirow_kept():
    """关键回归：\\multirow 里的 \\shortstack{a\\\\b} 内部 \\\\ 是 pandoc 崩表元凶。"""
    text = r"\multirow{3}{*}{\shortstack{UAV--\\Relay}}"
    out, _ = normalize_latex_for_pandoc(text)
    assert out == r"\multirow{3}{*}{UAV-- Relay}"   # multirow 壳保留，内部换行折平
    assert "\\\\" not in out                         # 不再有会撞行分隔符的 \\


def test_makecell_also_flattened():
    text = r"\makecell{a\\b\\c}"
    out, _ = normalize_latex_for_pandoc(text)
    assert out == "a b c"


def test_shortstack_with_optional_pos_arg():
    text = r"\shortstack[c]{甲\\乙}"
    out, _ = normalize_latex_for_pandoc(text)
    assert out == "甲 乙"


def test_rowbreak_with_spacing_param():
    text = r"\shortstack{甲\\[2pt]乙}"
    out, _ = normalize_latex_for_pandoc(text)
    assert out == "甲 乙"


def test_no_change_returns_original_and_no_notes():
    text = r"普通文本 $x=1$ \textbf{加粗} 无堆叠命令"
    out, notes = normalize_latex_for_pandoc(text)
    assert out == text
    assert notes == []


def test_does_not_touch_similar_command_names():
    """\\shortstackfoo 不是 \\shortstack，不应被处理。"""
    text = r"\shortstackfoo{a\\b}"
    out, _ = normalize_latex_for_pandoc(text)
    assert out == text


def test_real_table_row_becomes_pandoc_safe():
    """真机崩表的那一行：折平后不再含裸 \\\\（表格行分隔符之外的）冲突。"""
    row = r"\multirow{3}{*}{\shortstack{UAV--\\Relay}} & Low & 174 & $s \in [0.01,0.05]$ & 0.856 \\"
    out, _ = normalize_latex_for_pandoc(row)
    # 行尾真正的行分隔符 \\ 保留，单元格内的堆叠 \\ 被折平。
    assert out.endswith(r"0.856 \\")
    assert r"UAV-- Relay" in out
    assert r"\shortstack" not in out
