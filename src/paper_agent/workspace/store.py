"""工作区持久化后端。

`WorkspaceStore` 是可插拔的持久化接口；`JsonFileStore` 为默认实现
（本地 JSON 文件，零依赖，便于骨架与调试）。

持久化失败时 `save` 必须抛出 `PersistenceError`，由仓储层负责回滚内存状态
（Req 9.3 / Property 3）。
"""

from __future__ import annotations

import json
import os
import tempfile
from typing import Protocol, runtime_checkable

from paper_agent.workspace.models import PaperWorkspace


class PersistenceError(Exception):
    """持久化保存失败时抛出。"""


@runtime_checkable
class WorkspaceStore(Protocol):
    """工作区持久化接口。"""

    def load(self, workspace_id: str) -> PaperWorkspace | None:
        """加载工作区；不存在时返回 None。"""
        ...

    def save(self, ws: PaperWorkspace) -> None:
        """持久化保存工作区；失败时抛出 PersistenceError。"""
        ...


class JsonFileStore:
    """将工作区保存为本地 JSON 文件。

    采用「写临时文件 + 原子替换」保证落盘的原子性，避免写入中途崩溃
    导致文件损坏。
    """

    def __init__(self, root_dir: str) -> None:
        self._root = root_dir

    def _path(self, workspace_id: str) -> str:
        return os.path.join(self._root, f"{workspace_id}.json")

    def load(self, workspace_id: str) -> PaperWorkspace | None:
        path = self._path(workspace_id)
        if not os.path.exists(path):
            return None
        try:
            with open(path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
            return PaperWorkspace.from_dict(data)
        except (OSError, ValueError, KeyError) as exc:
            raise PersistenceError(f"加载工作区失败: {workspace_id}") from exc

    def save(self, ws: PaperWorkspace) -> None:
        try:
            os.makedirs(self._root, exist_ok=True)
            path = self._path(ws.workspace_id)
            payload = json.dumps(ws.to_dict(), ensure_ascii=False, indent=2)
            # 写临时文件后原子替换。
            fd, tmp = tempfile.mkstemp(dir=self._root, suffix=".tmp")
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as fh:
                    fh.write(payload)
                os.replace(tmp, path)
            finally:
                if os.path.exists(tmp):
                    os.remove(tmp)
        except OSError as exc:
            raise PersistenceError(
                f"保存工作区失败: {ws.workspace_id}"
            ) from exc


class InMemoryStore:
    """内存存储，主要用于测试。可注入失败开关以验证回滚逻辑。"""

    def __init__(self) -> None:
        self._data: dict[str, dict] = {}
        self.fail_on_save = False

    def load(self, workspace_id: str) -> PaperWorkspace | None:
        raw = self._data.get(workspace_id)
        return PaperWorkspace.from_dict(raw) if raw is not None else None

    def save(self, ws: PaperWorkspace) -> None:
        if self.fail_on_save:
            raise PersistenceError("注入的保存失败")
        # 存深拷贝（经由序列化），避免外部持有引用被改动。
        self._data[ws.workspace_id] = ws.to_dict()
