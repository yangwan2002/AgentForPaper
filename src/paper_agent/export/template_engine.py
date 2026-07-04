"""模板引擎（venue-templates-figures-tables）。

``TemplateEngine`` 按选定 ``Venue_Id`` 解析出 :class:`VenueProfile`，产出
:class:`Scaffold`（文档类声明、样式引用行、已落盘样式资产列表），并执行单次、
不级联的优雅回退（目标固定为 ``default``）。

关键契约（承接需求 1.4/2.1/2.2/2.3/2.5/3.1/3.2/3.4/3.5/3.6）：

- 解析 ``venue_id`` → ``registry.resolve``；只回退一次，回退目标恒为 ``default``。
- 回退触发条件（枚举 ``fallback_reason``）：
    * ``unregistered_venue``：``resolve`` 返回 ``None``。
    * ``invalid_profile``：``profile.is_valid()`` 为 ``False``。
    * ``missing_style_asset``：声明的 :class:`StyleAsset` 有 ``builtin_path`` 但
      对应文件不存在 / 无法加载。
- 回退时 ``Scaffold.degraded=True``、``fallback_reason`` 取枚举值、``degrade_note``
  为逐字节固定文本「已降级：请求的会议模板不可用，已回退到默认模板」，并经 ``sink``
  发出**恰一条** ``DEGRADATION`` 事件（``message`` = ``degrade_note``，``data`` 含
  ``feature="template"``、``reason``、``venue_id`` = 被请求的 id）。
- 若 ``default`` 亦不可用（无法 resolve / 校验失败 / 内置资产缺失）→ ``aborted=True``
  并发一条 ``ERROR`` 级事件（用现有 :data:`EventKind.AGENT_LOG` 表达，``message``
  指明「默认模板不可用」）。这是唯一的中止分支，且不落盘任何文档/资产。
- 落盘内置 :class:`StyleAsset`（``builtin_path`` 不为 ``None`` 者）到 ``out_dir``，
  绝对路径记入 ``asset_files``，并对每个落盘资产发一条 ``EXPORT_ASSET`` 事件。
- 全程**不调用任何 LLMProvider**（需求 3.4），不 ``eval``/``exec`` 任何不可信输入。

``TemplateEngine`` 只做纯数据 + 文件落盘，样式引用名写入前统一截断至 500 字符。
"""

from __future__ import annotations

import os
import shutil
from dataclasses import dataclass, field, replace

from paper_agent.observability.events import Event, EventKind, EventSink

from .venue_profiles import VenueProfile
from .venue_registry import VenueRegistry

# 样式引用名写入 .tex 前的长度上限（需求 2.5）。
_MAX_REF_CHARS = 500

# 可观测文本片段长度上限（需求 10.4）。
_MAX_EVENT_CHARS = 2000

# 回退目标固定为 default（需求 3.1/3.5，不级联）。
_DEFAULT_VENUE_ID = "default"

# 回退降级标注：逐字节固定文本（需求 3.2）。
_DEGRADE_NOTE = "已降级：请求的会议模板不可用，已回退到默认模板"

# 已知包选项映射：用于逐字节复现今日 latex.py 前导（inputenc 需要 [utf8]）。
# 键为 StyleAsset.name（引用声明名），值为选项列表。
_PACKAGE_OPTIONS: dict[str, list[str]] = {
    "inputenc": ["utf8"],
}


@dataclass
class Scaffold:
    """模板脚手架：模板引擎产出的纯数据结果。

    Attributes:
        document_class: LaTeX 文档类名（回退时为 ``default`` 的类名）；中止时为空串。
        preamble_lines: 前导行（``\\documentclass[...]{...}`` 与各 ``\\usepackage``）。
        asset_files: 已落盘样式资产的绝对路径列表。
        degraded: 是否发生模板回退降级。
        degrade_note: 回退时的逐字节固定降级文本；未回退为空串。
        requested_venue_id: 被请求的原始 ``Venue_Id``。
        fallback_reason: 回退原因，取值于
            ``{unregistered_venue, missing_style_asset, invalid_profile}``；未回退为 ``None``。
        aborted: ``default`` 亦不可用时为 ``True``（唯一中止分支，需求 3.6）。
    """

    document_class: str
    preamble_lines: list[str] = field(default_factory=list)
    asset_files: list[str] = field(default_factory=list)
    degraded: bool = False
    degrade_note: str = ""
    requested_venue_id: str = ""
    fallback_reason: str | None = None
    aborted: bool = False


class TemplateEngine:
    """按 ``Venue_Id`` 产出 :class:`Scaffold`；单次、不级联回退到 ``default``。"""

    def __init__(self, registry: VenueRegistry, sink: EventSink) -> None:
        self._registry = registry
        self._sink = sink

    # ------------------------------------------------------------------ #
    # 公共 API
    # ------------------------------------------------------------------ #
    def build_scaffold(
        self, venue_id: str, out_dir: str, styles_dir: str | None = None
    ) -> Scaffold:
        """解析 → 发现用户样式文件 → （回退）→ 落盘样式 → 返回 :class:`Scaffold`。

        ``styles_dir``（可选）为用户提供的会议样式文件目录（放置从会议官网/Overleaf
        下载的 ``.sty``/``.cls``）。对每个 ``requires_file`` 且尚无 ``builtin_path`` 的
        样式资产，在 ``styles_dir`` 中按 ``asset.filename`` 非递归查找；找到即作为其
        有效 ``builtin_path``。必需文件缺失（``requires_file`` 但无法定位）→ 触发
        ``missing_style_asset`` 回退到 ``default``。

        至多回退一次，回退目标固定为 ``default``。``default`` 亦不可用时返回
        ``aborted=True`` 的脚手架（不落盘任何文档/资产）。
        """
        requested = venue_id if isinstance(venue_id, str) else str(venue_id)

        # 先按 styles_dir 解析用户样式文件，再判定可用性（需求：解析必须早于可用性检查）。
        profile = self._resolve_profile(self._registry.resolve(venue_id), styles_dir)
        reason = self._unavailable_reason(profile)

        # 目标可用：直接产出，无降级。
        if reason is None:
            assert profile is not None  # _unavailable_reason 保证
            os.makedirs(out_dir, exist_ok=True)
            return self._assemble(
                profile, out_dir, requested_venue_id=requested, degraded=False
            )

        # 需要回退：单次解析 default，不级联。
        default_profile = self._resolve_profile(
            self._registry.resolve(_DEFAULT_VENUE_ID), styles_dir
        )
        if self._unavailable_reason(default_profile) is not None:
            # 唯一中止分支（需求 3.6）：不落盘、发 ERROR 级事件、输入不被修改。
            self._emit(
                Event(
                    kind=EventKind.AGENT_LOG,
                    message=_truncate("默认模板不可用，已中止本次导出"),
                    data={
                        "feature": "template",
                        "level": "error",
                        "reason": reason,
                        "venue_id": _truncate(requested),
                    },
                )
            )
            return Scaffold(
                document_class="",
                preamble_lines=[],
                asset_files=[],
                degraded=False,
                degrade_note="",
                requested_venue_id=requested,
                fallback_reason=reason,
                aborted=True,
            )

        assert default_profile is not None  # 上面已校验可用
        os.makedirs(out_dir, exist_ok=True)

        # 恰一条 DEGRADATION 事件（需求 3.2/3.5/10.2）。
        self._emit(
            Event(
                kind=EventKind.DEGRADATION,
                message=_truncate(_DEGRADE_NOTE),
                data={
                    "feature": "template",
                    "reason": reason,
                    "venue_id": _truncate(requested),
                },
            )
        )

        scaffold = self._assemble(
            default_profile, out_dir, requested_venue_id=requested, degraded=True
        )
        scaffold.degrade_note = _DEGRADE_NOTE
        scaffold.fallback_reason = reason
        return scaffold

    # ------------------------------------------------------------------ #
    # 内部实现
    # ------------------------------------------------------------------ #
    @staticmethod
    def _resolve_profile(
        profile: VenueProfile | None, styles_dir: str | None
    ) -> VenueProfile | None:
        """据 ``styles_dir`` 解析用户提供的样式文件，返回样式资产已解析的档案副本。

        对每个 ``requires_file`` 且尚无 ``builtin_path`` 的资产：若提供了 ``styles_dir``
        且资产声明了 ``filename``，在 ``styles_dir`` 内按 basename 非递归查找；命中则把
        其绝对路径作为该资产的有效 ``builtin_path``。不修改注册表中的原档案（返回副本）。
        """
        if profile is None:
            return None
        resolved_assets = []
        for asset in profile.style_assets:
            if (
                asset.requires_file
                and asset.builtin_path is None
                and styles_dir
                and asset.filename
            ):
                candidate = os.path.join(styles_dir, asset.filename)
                if os.path.isfile(candidate):
                    asset = replace(asset, builtin_path=os.path.abspath(candidate))
            resolved_assets.append(asset)
        return replace(profile, style_assets=resolved_assets)

    def _unavailable_reason(self, profile: VenueProfile | None) -> str | None:
        """判定 profile 是否不可用，返回回退原因或 ``None``（可用）。"""
        if profile is None:
            return "unregistered_venue"
        if not profile.is_valid():
            return "invalid_profile"
        if self._has_missing_style_asset(profile):
            return "missing_style_asset"
        return None

    @staticmethod
    def _has_missing_style_asset(profile: VenueProfile) -> bool:
        """判定档案是否缺样式资产（须在 ``styles_dir`` 解析之后调用）。

        缺失判据（择一即缺）：
        - 资产显式声明了 ``builtin_path`` 但对应文件不存在 / 无法加载；
        - 资产 ``requires_file`` 但未解析到任何文件（``builtin_path`` 仍为 ``None``）。

        系统包（``requires_file=False`` 且 ``builtin_path is None``，如 inputenc/graphicx）
        与 TeX 发行版自带类（IEEEtran）永不视为缺失。
        """
        for asset in profile.style_assets:
            path = asset.builtin_path
            if path is not None and not os.path.isfile(path):
                return True
            if asset.requires_file and path is None:
                return True
        return False

    def _assemble(
        self,
        profile: VenueProfile,
        out_dir: str,
        *,
        requested_venue_id: str,
        degraded: bool,
    ) -> Scaffold:
        """落盘内置资产并产出前导行，组装 :class:`Scaffold`。"""
        asset_files: list[str] = []
        preamble: list[str] = [self._documentclass_line(profile)]

        for asset in profile.style_assets:
            # 有已解析文件（内置或来自 styles_dir）则落盘：文件名优先用 asset.filename，
            # 否则回退到 builtin_path 的 basename（需求 2.2/2.3）。
            if asset.builtin_path is not None:
                landed_name = asset.filename or os.path.basename(asset.builtin_path)
                dest = os.path.join(out_dir, landed_name)
                shutil.copyfile(asset.builtin_path, dest)
                abs_dest = os.path.abspath(dest)
                asset_files.append(abs_dest)
                self._emit(
                    Event(
                        kind=EventKind.EXPORT_ASSET,
                        message=_truncate(f"落盘样式资产：{landed_name}"),
                        data={
                            "feature": "template",
                            "kind": asset.kind,
                            "name": _truncate(landed_name),
                            "path": _truncate(abs_dest),
                        },
                    )
                )

            # 仅 usepackage==True 的资产发 \usepackage 行；引用名用 asset.name（无扩展名），
            # 截断至 500 字符。.cls 文档类（usepackage==False）经 \documentclass 加载，不发。
            if asset.usepackage:
                ref_name = asset.name[:_MAX_REF_CHARS]
                preamble.append(self._usepackage_line(asset.name, ref_name))

        return Scaffold(
            document_class=profile.document_class,
            preamble_lines=preamble,
            asset_files=asset_files,
            degraded=degraded,
            degrade_note="",
            requested_venue_id=requested_venue_id,
            fallback_reason=None,
            aborted=False,
        )

    @staticmethod
    def _documentclass_line(profile: VenueProfile) -> str:
        """产出 ``\\documentclass[opts]{class}``；无选项时省略方括号。"""
        if profile.class_options:
            opts = ",".join(profile.class_options)
            return rf"\documentclass[{opts}]{{{profile.document_class}}}"
        return rf"\documentclass{{{profile.document_class}}}"

    @staticmethod
    def _usepackage_line(asset_name: str, ref_name: str) -> str:
        """产出 ``\\usepackage[opts]{ref}``；已知包（如 inputenc）带默认选项。"""
        options = _PACKAGE_OPTIONS.get(asset_name)
        if options:
            return rf"\usepackage[{','.join(options)}]{{{ref_name}}}"
        return rf"\usepackage{{{ref_name}}}"

    def _emit(self, event: Event) -> None:
        """安全地发事件：sink 异常不得影响脚手架产出。"""
        try:
            self._sink.emit(event)
        except Exception:  # noqa: BLE001 - 可观测性不得中断主流程
            pass


def _truncate(text: str, limit: int = _MAX_EVENT_CHARS) -> str:
    """对写入事件的文本片段做防御式截断（需求 10.4）。"""
    if not isinstance(text, str):
        text = str(text)
    return text[:limit]
