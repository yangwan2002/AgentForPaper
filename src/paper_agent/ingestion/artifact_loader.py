"""ResearchArtifact 加载器（Round 7）：从目录读 YAML + CSV，构造结构化输入。

约定目录结构（最低限）::

    my_paper/
    ├── artifact.yaml         # 主清单（必需）
    ├── experiments/
    │   ├── main.csv          # artifact.yaml 引用的实验数据
    │   └── ablation.csv
    └── notes.md              # 可选：自由格式补充说明（自动加到 artifact.notes）

`artifact.yaml` 必填字段（缺则 ``ArtifactLoadError``）：

- ``research_question``: 一句话研究问题
- ``method.overview``: 方法概述
- ``contributions``: 至少 1 条
- ``experiments``: 至少 1 条；每条须含 ``experiment_id``，``results_csv`` 可选
  （但建议有——没有就走不了 grounding 检查）

CSV 解析采用 stdlib ``csv`` 模块，零依赖。YAML 解析优先用 ``pyyaml``（声明在
``[artifact]`` extra）；若用户给的是合法 JSON 也接受（YAML 1.2 是 JSON 超集）。
"""

from __future__ import annotations

import csv
import json
import os
from typing import Any

from paper_agent.workspace.research_artifact import (
    Contribution,
    Experiment,
    MethodSpec,
    ResearchArtifact,
)


class ArtifactLoadError(Exception):
    """加载 artifact 失败（文件缺失、解析错误、必填字段缺失）。"""


_ARTIFACT_FILENAME_CANDIDATES = (
    "artifact.yaml",
    "artifact.yml",
    "artifact.json",
)


def _load_structured(path: str) -> dict:
    """读 ``.yaml`` / ``.yml`` / ``.json`` 文件为 dict；其他后缀报错。

    YAML 解析需要 ``pyyaml``（[artifact] extra）；不可用时若文件能被 JSON 解析
    则回退到 JSON——YAML 1.2 是 JSON 超集，纯 JSON 可双向工作。
    """
    ext = os.path.splitext(path)[1].lower()
    if ext in (".json",):
        with open(path, "r", encoding="utf-8-sig") as fh:
            return json.load(fh)
    if ext in (".yaml", ".yml"):
        try:
            import yaml  # noqa: WPS433 - 可选依赖
        except ImportError:
            # 尝试当作 JSON 解析（用户写的就是 JSON 子集时仍能用）。
            with open(path, "r", encoding="utf-8-sig") as fh:
                text = fh.read()
            try:
                return json.loads(text)
            except json.JSONDecodeError as exc:
                raise ArtifactLoadError(
                    f"读取 YAML 需要 pyyaml：pip install '.[artifact]'。"
                    f"或将 {path} 转为 JSON。原始错误：{exc}"
                ) from exc
        with open(path, "r", encoding="utf-8-sig") as fh:
            data = yaml.safe_load(fh)
        return data if isinstance(data, dict) else {}
    raise ArtifactLoadError(
        f"不支持的 artifact 主清单格式：{ext}（仅支持 .yaml/.yml/.json）"
    )


def _read_csv(path: str) -> dict[str, Any]:
    """把 CSV 读成 ``{"columns": [...], "rows": [{col: val}, ...], "stats": {...}}``。

    数值列在 ``stats`` 中给出 ``mean / std / min / max``——非数值列略过。
    """
    with open(path, "r", encoding="utf-8-sig", newline="") as fh:
        reader = csv.DictReader(fh)
        columns = list(reader.fieldnames or [])
        rows: list[dict[str, Any]] = []
        for raw_row in reader:
            row: dict[str, Any] = {}
            for col in columns:
                val = raw_row.get(col, "")
                if val is None:
                    row[col] = None
                    continue
                val_str = val.strip() if isinstance(val, str) else val
                # 尝试解析为数值；不能则保留字符串。
                row[col] = _try_number(val_str)
            rows.append(row)

    # 计算数值列的统计。
    stats: dict[str, dict[str, float]] = {}
    for col in columns:
        numeric_vals: list[float] = []
        for row in rows:
            v = row.get(col)
            if isinstance(v, (int, float)) and not isinstance(v, bool):
                numeric_vals.append(float(v))
        if not numeric_vals:
            continue
        n = len(numeric_vals)
        mean = sum(numeric_vals) / n
        var = sum((x - mean) ** 2 for x in numeric_vals) / n
        std = var ** 0.5
        stats[col] = {
            "mean": mean,
            "std": std,
            "min": min(numeric_vals),
            "max": max(numeric_vals),
            "n": float(n),
        }

    return {"columns": columns, "rows": rows, "stats": stats}


def _try_number(val: Any) -> Any:
    """尝试把字符串解析为 int / float；失败返回原值。"""
    if not isinstance(val, str):
        return val
    s = val.strip()
    if not s:
        return ""
    # 优先 int（避免 "42" 被读成 42.0）。
    try:
        return int(s)
    except ValueError:
        pass
    try:
        return float(s)
    except ValueError:
        return s


def _find_artifact_file(root: str) -> str:
    for name in _ARTIFACT_FILENAME_CANDIDATES:
        path = os.path.join(root, name)
        if os.path.isfile(path):
            return path
    raise ArtifactLoadError(
        f"目录 {root} 中找不到 artifact 主清单（在以下文件名中寻找："
        f"{', '.join(_ARTIFACT_FILENAME_CANDIDATES)}）。"
    )


def _validate_required(data: dict, root: str) -> None:
    """必填字段校验——缺则报错（不静默回退，避免 GENERATION 模式编一份假货）。"""
    missing: list[str] = []
    if not str(data.get("research_question") or "").strip():
        missing.append("research_question")
    method = data.get("method") or {}
    if not str(method.get("overview") or "").strip():
        missing.append("method.overview")
    contributions = data.get("contributions") or []
    if not contributions:
        missing.append("contributions（至少 1 条）")
    experiments = data.get("experiments") or []
    if not experiments:
        missing.append("experiments（至少 1 条）")
    for i, exp in enumerate(experiments):
        if not isinstance(exp, dict):
            missing.append(f"experiments[{i}]（应为对象）")
            continue
        if not str(exp.get("experiment_id") or "").strip():
            missing.append(f"experiments[{i}].experiment_id")
    if missing:
        raise ArtifactLoadError(
            f"artifact 缺少必填字段（在 {root}）：" + "；".join(missing)
        )


def load_artifact(artifact_dir: str) -> ResearchArtifact:
    """从目录加载 ``ResearchArtifact``。

    Args:
        artifact_dir: 含 ``artifact.yaml``（或 .yml/.json）的目录路径。

    Returns:
        构造好的 ``ResearchArtifact``，``experiments[i].results_data`` 已填入
        CSV 解析结果（含数值列的 mean/std/min/max）。

    Raises:
        ArtifactLoadError: 目录不存在 / 找不到主清单 / 解析失败 / 必填字段缺失。
    """
    if not os.path.isdir(artifact_dir):
        raise ArtifactLoadError(f"artifact 目录不存在：{artifact_dir}")

    artifact_path = _find_artifact_file(artifact_dir)
    try:
        data = _load_structured(artifact_path)
    except ArtifactLoadError:
        raise
    except Exception as exc:  # noqa: BLE001 - 任何 YAML/JSON 错误归一化
        raise ArtifactLoadError(
            f"解析 {artifact_path} 失败：{exc}"
        ) from exc
    if not isinstance(data, dict):
        raise ArtifactLoadError(
            f"{artifact_path} 顶层必须是对象（dict），实际：{type(data).__name__}"
        )

    _validate_required(data, artifact_dir)

    # 注入 artifact 根目录，便于 CSV 相对路径解析。
    data["artifact_dir"] = os.path.abspath(artifact_dir)

    # 解析 experiments 的 CSV（缺失/读不到 → 留空 results_data 但不报错）。
    experiments_data = data.get("experiments") or []
    for exp in experiments_data:
        csv_rel = (exp.get("results_csv") or "").strip()
        if not csv_rel:
            continue
        csv_path = (
            csv_rel if os.path.isabs(csv_rel)
            else os.path.join(artifact_dir, csv_rel)
        )
        if not os.path.isfile(csv_path):
            # 不报错——CSV 缺失不算致命，但 grounding 检查会发现没有数值兜底。
            exp["results_data"] = {
                "columns": [],
                "rows": [],
                "stats": {},
                "_error": f"CSV 文件不存在：{csv_path}",
            }
            continue
        try:
            exp["results_data"] = _read_csv(csv_path)
        except Exception as exc:  # noqa: BLE001
            exp["results_data"] = {
                "columns": [],
                "rows": [],
                "stats": {},
                "_error": f"CSV 读取失败：{exc}",
            }

    # 自动加载 notes.md（可选；存在则附加到 artifact.notes）。
    notes_path = os.path.join(artifact_dir, "notes.md")
    if os.path.isfile(notes_path):
        try:
            with open(notes_path, "r", encoding="utf-8-sig") as fh:
                user_notes = fh.read().strip()
            existing = (data.get("notes") or "").strip()
            data["notes"] = (existing + "\n\n" + user_notes).strip() if existing else user_notes
        except OSError:
            pass  # notes 读取失败不阻断

    return ResearchArtifact.from_dict(data)


__all__ = ["ArtifactLoadError", "load_artifact"]
