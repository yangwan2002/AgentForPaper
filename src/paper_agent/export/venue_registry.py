"""会议档案注册表（venue-templates-figures-tables）。

``VenueRegistry`` 把归一化后的 ``Venue_Id`` 映射到内置 :class:`VenueProfile`，
供 ``Template_Engine`` 在导出阶段解析模板。注册表本身零 I/O、零副作用
（样式落盘由 ``Template_Engine`` 负责），便于独立测试。

内置档案：``neurips`` / ``icml`` / ``acl`` / ``ieee`` / ``default``。其中
``default`` 逐字节对应今日 ``export/latex.py`` 的前导：

    \\documentclass{article}
    \\usepackage[utf8]{inputenc}
    \\usepackage{graphicx}

即 ``document_class="article"``，``style_assets`` 为 ``inputenc`` / ``graphicx``
的引用声明（``builtin_path=None``，仅引用、无内置文件）。
"""

from __future__ import annotations

from .venue_profiles import StyleAsset, VenueProfile


def _build_builtin_profiles() -> dict[str, VenueProfile]:
    """构造内置会议档案表（键为已归一化的 ``Venue_Id``）。"""

    # default：复现现行 article 前导。inputenc/graphicx 为系统包，无需用户文件。
    default = VenueProfile(
        venue_id="default",
        document_class="article",
        class_options=[],
        style_assets=[
            StyleAsset(
                name="inputenc", builtin_path=None, kind="sty",
                requires_file=False, usepackage=True,
            ),
            StyleAsset(
                name="graphicx", builtin_path=None, kind="sty",
                requires_file=False, usepackage=True,
            ),
        ],
        required_structure=["title", "authors", "body"],
        docx_conventions={},
    )

    neurips = VenueProfile(
        venue_id="neurips",
        document_class="neurips_2024",
        class_options=[],
        style_assets=[
            StyleAsset(
                name="neurips_2024", filename="neurips_2024.sty", kind="sty",
                requires_file=True, usepackage=True,
            ),
        ],
        required_structure=["title", "authors", "abstract", "body"],
        docx_conventions={"heading_style": "Heading"},
    )

    icml = VenueProfile(
        venue_id="icml",
        document_class="icml2024",
        class_options=["accepted"],
        style_assets=[
            StyleAsset(
                name="icml2024", filename="icml2024.sty", kind="sty",
                requires_file=True, usepackage=True,
            ),
        ],
        required_structure=["title", "authors", "abstract", "body"],
        docx_conventions={"heading_style": "Heading"},
    )

    acl = VenueProfile(
        venue_id="acl",
        document_class="article",
        class_options=["11pt"],
        style_assets=[
            StyleAsset(
                name="acl", filename="acl.sty", kind="sty",
                requires_file=True, usepackage=True,
            ),
        ],
        required_structure=["title", "authors", "abstract", "body"],
        docx_conventions={"heading_style": "Heading"},
    )

    ieee = VenueProfile(
        venue_id="ieee",
        document_class="IEEEtran",
        class_options=["conference"],
        style_assets=[
            # IEEEtran 随 TeX Live 发行，是文档类（\documentclass），不 \usepackage。
            StyleAsset(
                name="IEEEtran", filename="IEEEtran.cls", kind="cls",
                requires_file=False, usepackage=False,
            ),
        ],
        required_structure=["title", "authors", "abstract", "body"],
        docx_conventions={"heading_style": "Heading"},
    )

    return {p.venue_id: p for p in (default, neurips, icml, acl, ieee)}


def _normalize(venue_id: str) -> str:
    """把 ``venue_id`` 归一化为查表键：去首尾空白后转小写。"""
    return venue_id.strip().lower()


class VenueRegistry:
    """内置会议档案注册表。

    解析时对 ``venue_id`` 做 ``strip`` + ``lowercase`` 归一化后匹配；
    未注册返回 ``None``（由 ``Template_Engine`` 转为回退）。
    """

    def __init__(self) -> None:
        self._profiles: dict[str, VenueProfile] = _build_builtin_profiles()

    def resolve(self, venue_id: str) -> VenueProfile | None:
        """返回已注册档案；未注册返回 ``None``（触发回退）。

        对 ``venue_id`` 做 ``strip`` + ``lowercase`` 归一后匹配。非字符串
        或归一后为空串一律视为未注册，返回 ``None``。
        """
        if not isinstance(venue_id, str):
            return None
        key = _normalize(venue_id)
        if not key:
            return None
        return self._profiles.get(key)

    def registered_ids(self) -> set[str]:
        """已登记的 ``Venue_Id`` 集合（至少含 neurips/icml/acl/ieee/default）。"""
        return set(self._profiles.keys())
