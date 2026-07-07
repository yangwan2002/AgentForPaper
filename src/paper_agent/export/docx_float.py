"""把 docx 内联图片改成**浮动锚定、跨栏满宽、锚定页顶、上下环绕**（visual-layout-acceptance）。

对标 LaTeX ``figure*[t]`` 的视觉效果：图独占页顶整宽、文字在其下方回流。做法是把 pandoc
产出的 ``<wp:inline>`` 内联图转换成 ``<wp:anchor>`` 浮动图，并设：
- ``positionV relativeFrom="page" align="top"``（锚定所在页页顶）；
- ``positionH relativeFrom="margin" align="center"``（水平居中）；
- ``wrapTopAndBottom``（文字在图上下回流，不并排）；
- 可选把图放大到版心整宽（``span_columns``）。

诚实边界（见对话记录）：这产出的效果 = 用户在 Word 里手动摆浮动图的效果，确定性、可复现。
但"图落在第几页"由内容流决定（Word 无 LaTeX 那种自动挑页的浮动算法）——本函数只保证
"锚到它所在那一页的页顶"；要精确落到"下一页顶"，用 ``force_next_page``（前置分页符，代价是
上一页底可能留白），或交由上层的视觉验收闭环渲染反馈微调。

只把 inline 换成 anchor、不增删段落/图形，故结构计数不变（对 Preservation_Check 安全）。
"""

from __future__ import annotations

_TWIP_TO_EMU = 635  # 1 twip = 635 EMU


def _section_text_width_twips(document) -> int:
    """版心宽（页宽 − 左右页边距）→ twips；取不到回退 A4 版心近似。"""
    try:
        sec = document.sections[0]
        twips = (int(sec.page_width) - int(sec.left_margin) - int(sec.right_margin)) // _TWIP_TO_EMU
        if 1440 <= twips <= 20000:
            return twips
    except Exception:  # noqa: BLE001
        pass
    return 9200


def _iter_body_paragraphs(document):
    return list(document.paragraphs)


def _collect_inline_drawings(document, qn):
    """按文档顺序收集所有含 ``<wp:inline>`` 图片的 (paragraph, drawing, inline)。"""
    found = []
    for para in _iter_body_paragraphs(document):
        for run in para.runs:
            drawing = run._r.find(qn("w:drawing"))
            if drawing is None:
                continue
            inline = drawing.find(qn("wp:inline"))
            if inline is not None:
                found.append((para, drawing, inline))
    return found


def float_figure_top(
    docx_path: str,
    *,
    index: int = 1,
    span_columns: bool = True,
    force_next_page: bool = False,
    text_width_twips: int | None = None,
) -> tuple[bool, str]:
    """把第 ``index`` 张内联图改成浮动、页顶、（可选满宽）、上下环绕。

    返回 ``(是否成功, 说明)``；找不到图/依赖缺失时返回 ``(False, 原因)``，不抛。
    """
    try:
        import docx  # noqa: WPS433
        from docx.oxml import OxmlElement
        from docx.oxml.ns import qn
    except Exception as exc:  # noqa: BLE001 - 无 python-docx
        return False, f"需要 python-docx：{type(exc).__name__}"

    try:
        document = docx.Document(docx_path)
    except Exception as exc:  # noqa: BLE001
        return False, f"无法打开 docx：{type(exc).__name__}"

    drawings = _collect_inline_drawings(document, qn)
    if index < 1 or index > len(drawings):
        return False, f"未找到第 {index} 张内联图片（共 {len(drawings)} 张）。"

    para, drawing, inline = drawings[index - 1]

    if text_width_twips is None:
        text_width_twips = _section_text_width_twips(document)
    target_cx = int(text_width_twips) * _TWIP_TO_EMU

    extent = inline.find(qn("wp:extent"))
    try:
        cx = int(extent.get("cx"))
        cy = int(extent.get("cy"))
    except (TypeError, ValueError, AttributeError):
        return False, "图片尺寸信息缺失，无法处理。"

    if span_columns and cx > 0:
        new_cx = target_cx
        new_cy = max(1, int(cy * target_cx / cx))
    else:
        new_cx, new_cy = cx, cy

    # 构造 wp:anchor（属性为非限定名）。
    anchor = OxmlElement("wp:anchor")
    for name, val in (
        ("distT", "0"), ("distB", "0"), ("distL", "0"), ("distR", "0"),
        ("simplePos", "0"), ("relativeHeight", "251658240"),
        ("behindDoc", "0"), ("locked", "0"), ("layoutInCell", "1"), ("allowOverlap", "1"),
    ):
        anchor.set(name, val)

    simple_pos = OxmlElement("wp:simplePos"); simple_pos.set("x", "0"); simple_pos.set("y", "0")
    pos_h = OxmlElement("wp:positionH"); pos_h.set("relativeFrom", "margin")
    ah = OxmlElement("wp:align"); ah.text = "center"; pos_h.append(ah)
    pos_v = OxmlElement("wp:positionV"); pos_v.set("relativeFrom", "page")
    av = OxmlElement("wp:align"); av.text = "top"; pos_v.append(av)
    new_extent = OxmlElement("wp:extent"); new_extent.set("cx", str(new_cx)); new_extent.set("cy", str(new_cy))
    wrap = OxmlElement("wp:wrapTopAndBottom")

    for child in (simple_pos, pos_h, pos_v, new_extent, wrap):
        anchor.append(child)
    # 从 inline 迁移 docPr / cNvGraphicFramePr / graphic（lxml append 会自动从原父节点摘下）。
    for tag in ("wp:docPr", "wp:cNvGraphicFramePr", "a:graphic"):
        el = inline.find(qn(tag))
        if el is not None:
            anchor.append(el)

    # 同步图片自身尺寸（a:ext）到新尺寸，否则图仍按旧尺寸渲染。
    if span_columns:
        for ext in anchor.iter(qn("a:ext")):
            ext.set("cx", str(new_cx))
            ext.set("cy", str(new_cy))
            break

    drawing.remove(inline)
    drawing.append(anchor)

    if force_next_page:
        p_pr = para._p.get_or_add_pPr()
        if p_pr.find(qn("w:pageBreakBefore")) is None:
            p_pr.insert(0, OxmlElement("w:pageBreakBefore"))

    try:
        document.save(docx_path)
    except Exception as exc:  # noqa: BLE001
        return False, f"保存 docx 失败：{type(exc).__name__}"

    where = "下一页顶部" if force_next_page else "所在页页顶"
    span = "、跨栏满宽" if span_columns else ""
    return True, f"已把第 {index} 张图设为浮动、锚定{where}{span}、文字上下环绕。"


__all__ = ["float_figure_top"]
