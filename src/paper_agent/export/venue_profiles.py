"""会议档案纯数据模型（venue-templates-figures-tables）。

本模块只定义零副作用的纯数据 dataclass，供 ``VenueRegistry`` 与
``Template_Engine`` 使用，便于独立测试。不做任何 I/O，不调用 LLM，
不 ``eval``/``exec`` 任何不可信输入。
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class StyleAsset:
    """LaTeX 样式资产引用（.sty/.cls/.bst）。

    Attributes:
        name: ``\\usepackage`` 引用标识（**不含扩展名**，如 ``"neurips_2024"``）；
            写入 ``.tex`` 前截断至 500 字符。
        builtin_path: 内置文件绝对路径；``None`` 表示尚未解析到具体文件
            （可能需从 ``styles_dir`` 发现，或仅为系统包引用声明）。
        kind: 资产类型，取值 ``sty`` | ``cls`` | ``bst``。
        filename: 实际文件名（含扩展名，如 ``"neurips_2024.sty"``）；用于在
            ``styles_dir`` 中查找、以及命名落盘文件。``None`` 表示无对应文件
            （系统包 / TeX 发行版自带类）。
        requires_file: 是否**必须**有用户提供的文件才能编译（会议专有包为 ``True``）；
            系统包（inputenc/graphicx）与 TeX 发行版自带类（IEEEtran）为 ``False``。
        usepackage: 是否发出 ``\\usepackage`` 行；``.cls`` 文档类为 ``False``
            （经 ``\\documentclass`` 加载，不 ``\\usepackage``）。
    """

    name: str
    builtin_path: str | None = None
    kind: str = "sty"
    filename: str | None = None
    requires_file: bool = False
    usepackage: bool = True


@dataclass
class VenueProfile:
    """会议档案：把一个 ``Venue_Id`` 映射到导出所需的模板信息。

    Attributes:
        venue_id: 会议标识（``neurips`` | ``icml`` | ``acl`` | ``ieee`` | ``default``）。
        document_class: LaTeX 文档类名（如 ``"article"`` / ``"neurips_2024"``）。
        class_options: 文档类选项列表。
        style_assets: 该档案声明的样式资产列表。
        required_structure: 必需结构元素（如 ``title`` / ``authors`` / ``body``）。
        docx_conventions: docx 约定（如标题层级映射）。
    """

    venue_id: str
    document_class: str
    class_options: list[str] = field(default_factory=list)
    style_assets: list[StyleAsset] = field(default_factory=list)
    required_structure: list[str] = field(default_factory=list)
    docx_conventions: dict = field(default_factory=dict)

    def is_valid(self) -> bool:
        """判断档案是否合法（``invalid_profile`` 判据）。

        当 ``document_class`` 非空（去除首尾空白后非空）且 ``required_structure``
        完整——即非空且其中每个元素都是非空字符串——时返回 ``True``；
        否则返回 ``False``。
        """
        if not self.document_class or not self.document_class.strip():
            return False
        if not self.required_structure:
            return False
        return all(
            isinstance(item, str) and item.strip()
            for item in self.required_structure
        )
