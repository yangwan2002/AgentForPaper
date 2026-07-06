"""变化页选择（visual-layout-acceptance · Task 4）。

docx 没有固定页码、且一处版面改动会令后文回流连带改变多页，故「改动页」只能靠
**渲染后比对**得到。因视觉验收在编辑后触发、天然同时持有编辑前 / 后两版渲染图，
据此逐页比对，只把**真正变化的页**（含少量邻页上下文）送多模态模型——聚焦判断、
省 VLM 成本，取代「固定送前 N 页」。

比对策略：同一后端渲染，未变页的 PNG **逐字节一致** → 用文件内容哈希判等即可（快、准）；
不等即变化页。页数变化时多出来的 after 页也算变化页。
"""

from __future__ import annotations

import hashlib


def _file_hash(path: str) -> str:
    """文件内容 SHA-256；读失败返回一个唯一占位（视为"与任何页都不同"→变化页）。"""
    try:
        with open(path, "rb") as fh:
            return hashlib.sha256(fh.read()).hexdigest()
    except OSError:
        return f"__unreadable__:{path}"


def _changed_indices(before: list[str], after: list[str]) -> list[int]:
    """返回 after 中相对 before 发生变化的页序号（0-based）。"""
    changed: list[int] = []
    for i, a in enumerate(after):
        if i >= len(before) or _file_hash(a) != _file_hash(before[i]):
            changed.append(i)
    return changed


def _expand_with_neighbors(indices: list[int], total: int, neighbor: int) -> list[int]:
    """把变化页序号向前后各扩 ``neighbor`` 页作为上下文，去重并裁进 [0,total)。"""
    picked: set[int] = set()
    for i in indices:
        for j in range(i - neighbor, i + neighbor + 1):
            if 0 <= j < total:
                picked.add(j)
    return sorted(picked)


def select_pages_to_judge(
    before_images: list[str] | None,
    after_images: list[str],
    *,
    max_pages: int,
    neighbor: int = 1,
) -> tuple[list[str], bool]:
    """挑选送多模态模型的页面，返回 ``(页面路径列表, sampled_flag)``。

    - 有 ``before_images``：逐页比对挑出变化页 + 邻页上下文；若无任何变化，回退为前
      ``max_pages`` 页（给判断一个最小样本）。
    - 无 ``before_images``（如全新 tex→docx，无编辑前基线）：回退为前 ``max_pages`` 页。
    - 送出页数恒不超过 ``max_pages``；被截断时 ``sampled_flag=True``（裁定标注"仅采样"）。
    """
    total = len(after_images)
    if total == 0:
        return [], False
    cap = max(1, int(max_pages))

    if not before_images:
        selected = list(range(min(cap, total)))
        return [after_images[i] for i in selected], total > cap

    changed = _changed_indices(before_images, after_images)
    if not changed:
        # 前后完全一致（版面改动无视觉效果）→ 给个最小样本，不空手判断。
        selected = list(range(min(cap, total)))
        return [after_images[i] for i in selected], total > cap

    expanded = _expand_with_neighbors(changed, total, neighbor)
    sampled = len(expanded) > cap
    selected = expanded[:cap]
    return [after_images[i] for i in selected], sampled


__all__ = ["select_pages_to_judge"]
