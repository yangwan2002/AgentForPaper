"""修订路由与补丁应用数据模型（Part A：补丁优先增量修订）。

本模块承载 `WritingAgent` 修订路径的路由决策与补丁应用结果类型，
均为纯 dataclass / Enum，零外部依赖，便于测试与序列化。

设计要点（见 design.md「Data Models / 修订路由相关」）：
- `RevisionRoute`：本轮修订采用的路径（补丁优先 / 整章重写）。
- `FallbackReason`：从 Patch_Mode 回退到 Whole_Section_Regeneration 的原因枚举
  （Req 4.2 要求回退原因取值于该固定集合）。
- `PatchApplication`：一次针对某章节的补丁应用结果，含成功/跳过计数、
  所选路径、回退原因，以及用于重叠检测的已改动字符区间（Req 2.5）。

复用既有 `SectionEdit`（`workspace/models.py`），本模块不重定义、不修改其字段。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class RevisionRoute(str, Enum):
    """本轮修订采用的路径（Req 4.3）。"""

    PATCH_MODE = "patch_mode"                       # 补丁优先，产出最小化局部 diff
    WHOLE_SECTION = "whole_section_regeneration"    # 整章重写回退路径


class FallbackReason(str, Enum):
    """从 Patch_Mode 回退到 Whole_Section_Regeneration 的原因（Req 4.2）。

    取值固定于该集合，供可观测事件标识回退动因。
    """

    ANCHOR_NOT_UNIQUE = "锚点未唯一命中"      # 全部补丁锚点命中 0 或 >1（Req 3.1）
    STRUCTURAL_CHANGE = "结构型改动"          # 章节新增/删除/层级调整（Req 3.2）
    PATCH_SIZE_EXCEEDED = "超过补丁适用上限"   # 补丁累计影响占比 > patch_size_limit（Req 3.2）


@dataclass
class PatchApplication:
    """一次针对某章节的补丁应用结果。

    - ``section_id``：被修订的目标章节 id。
    - ``applied``：成功应用的补丁数量。
    - ``skipped``：被跳过的补丁数量（锚点未唯一命中 / 区间冲突等）。
    - ``route``：本次采用的修订路径。
    - ``fallback_reason``：若发生回退则标识原因，否则为 None。
    - ``changed_intervals``：已成功补丁改动的字符区间集合（半开区间 ``[start, end)``），
      供改动区间重叠检测使用（Req 2.5）。
    """

    section_id: str
    applied: int
    skipped: int
    route: RevisionRoute
    fallback_reason: FallbackReason | None = None
    changed_intervals: list[tuple[int, int]] = field(default_factory=list)
