"""工作区仓储。

封装对 `PaperWorkspace` 的读写，是智能体与持久化之间的唯一通道。

关键职责（Req 9 / Property 3）：
- update(): 接收一个修改函数，应用到内存副本后尝试落盘；
  若持久化失败，则回滚内存状态并抛出异常，保证内存与磁盘一致。
- 智能体不直接调用 store，统一经由仓储进行原子更新。
"""

from __future__ import annotations

import copy
from datetime import datetime, timezone
from typing import Callable

from paper_agent.workspace.models import PaperWorkspace
from paper_agent.workspace.store import WorkspaceStore


class WorkspaceRepository:
    def __init__(self, store: WorkspaceStore) -> None:
        self._store = store

    def load(self, workspace_id: str) -> PaperWorkspace | None:
        return self._store.load(workspace_id)

    def create(self, ws: PaperWorkspace) -> PaperWorkspace:
        """创建并持久化一个新工作区。"""
        self._store.save(ws)
        return ws

    def update(
        self, ws: PaperWorkspace, mutate: Callable[[PaperWorkspace], None]
    ) -> PaperWorkspace:
        """对工作区执行一次原子更新。

        将 mutate 应用到 ws；落盘成功则返回更新后的 ws；
        落盘失败则把 ws 恢复到更新前的状态并向上抛出异常
        （Req 9.3：阻止本次更新直至持久化成功）。
        """
        snapshot = copy.deepcopy(ws.to_dict())
        mutate(ws)
        ws.updated_at = datetime.now(timezone.utc)
        try:
            self._store.save(ws)
        except Exception:
            # 回滚内存状态，保证内存与磁盘一致。
            restored = PaperWorkspace.from_dict(snapshot)
            ws.__dict__.update(restored.__dict__)
            raise
        return ws
