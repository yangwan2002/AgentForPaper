"""会话持久化：把 ``AgentSession`` 的任务与 transcript 依托工作区持久化（Req 9.4/9.5）。

设计取舍：``session_id == workspace_id``，会话状态（任务描述、transcript）存入
``ws.profile`` 的保留键，随既有工作区 JSON 一并落盘。由此续跑无需另建持久化，
直接 ``repo.load(session_id)`` 即可恢复会话（Req 9.5）。
"""

from __future__ import annotations

from paper_agent.agent_platform.models import AgentSession, WritingTask
from paper_agent.workspace.models import PaperWorkspace
from paper_agent.workspace.repository import WorkspaceRepository

# ws.profile 中承载会话状态的保留键。
_TASK_KEY = "agent_task"
_TRANSCRIPT_KEY = "agent_transcript"


def save_session(repo: WorkspaceRepository, session: AgentSession) -> None:
    """把会话的任务与 transcript 写入工作区 profile 并原子落盘。"""
    task_data = _task_to_dict(session.task)
    transcript = list(session.transcript)

    def _mutate(ws: PaperWorkspace) -> None:
        ws.profile[_TASK_KEY] = task_data
        ws.profile[_TRANSCRIPT_KEY] = transcript

    repo.update(session.workspace, _mutate)


def load_session(
    repo: WorkspaceRepository, session_id: str
) -> AgentSession | None:
    """据 session_id（==workspace_id）恢复会话；不存在返回 None（Req 9.5）。"""
    ws = repo.load(session_id)
    if ws is None:
        return None
    task = _task_from_dict(ws.profile.get(_TASK_KEY))
    transcript = list(ws.profile.get(_TRANSCRIPT_KEY) or [])
    return AgentSession(
        session_id=session_id, workspace=ws, task=task, transcript=transcript
    )


def _task_to_dict(task: WritingTask) -> dict:
    """仅持久化可序列化字段（artifact 已在工作区独立持久化，不重复存）。"""
    return {
        "instruction": task.instruction,
        "workspace_id": task.workspace_id,
        "draft_path": task.draft_path,
        "topic_background": task.topic_background,
        "confirm_ingestion": task.confirm_ingestion,
    }


def _task_from_dict(data: dict | None) -> WritingTask:
    data = data or {}
    return WritingTask(
        instruction=str(data.get("instruction", "")),
        workspace_id=data.get("workspace_id"),
        draft_path=data.get("draft_path"),
        topic_background=data.get("topic_background"),
        confirm_ingestion=bool(data.get("confirm_ingestion", False)),
    )


__all__ = ["save_session", "load_session"]
