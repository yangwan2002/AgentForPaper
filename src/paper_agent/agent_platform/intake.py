"""意图/对话层：受理自然语言写作任务，初始化或续跑会话（Req 1 / 11）。

不做固定意图分类（Req 1.5）——任务原文直接作为 Agent_Loop 的目标。职责：
- 空/纯空白任务且无任何上下文 → 拒绝并提示（Req 1.4）；
- 无 instruction 但给了初稿/主题（Legacy 用法）→ 合成默认任务（Req 11.1/11.2）；
- 据初稿/主题初始化工作区上下文（复用既有输入模式判定）；
- 续跑：据 session_id 恢复既有会话（Req 9.5）。
"""

from __future__ import annotations

import os
import uuid
from dataclasses import replace

from paper_agent.agent_platform.models import AgentSession, WritingTask
from paper_agent.agent_platform.session_store import load_session, save_session
from paper_agent.orchestrator import InputValidationError
from paper_agent.workspace.models import InputMode, OutputFormat, PaperWorkspace
from paper_agent.workspace.repository import WorkspaceRepository

# Legacy 合成的默认任务（无 instruction 时按输入类型选择）。
_DEFAULT_DRAFT_INSTRUCTION = "请在保持原意与事实的前提下，修订并润色本文。"
_DEFAULT_TOPIC_INSTRUCTION = "请以该主题从零撰写一篇结构完整的学术论文。"


class TaskIntake:
    """任务受理器。"""

    def __init__(
        self,
        repo: WorkspaceRepository,
        *,
        default_output_format: OutputFormat = OutputFormat.MARKDOWN,
        draft_loader=None,
    ) -> None:
        self._repo = repo
        self._default_output = default_output_format
        # 初稿文件加载器（可注入，默认惰性用 ingestion.load_document）。
        self._draft_loader = draft_loader

    def start(
        self, task: WritingTask, *, require_instruction: bool = True
    ) -> AgentSession:
        """受理一个新任务，返回可运行的会话。

        ``require_instruction=False``（对话模式）时允许空 instruction——此时开一个
        空会话，首条指令由后续对话轮提供，不做空任务拒绝。
        """
        has_context = bool(
            task.draft_path or task.topic_background or task.workspace_id
        )
        if require_instruction and not task.has_instruction() and not has_context:
            raise InputValidationError(
                "请提供任务描述，或至少给出初稿文件 / 主题 / 已有工作区之一。"
            )

        if task.has_instruction():
            instruction = task.instruction
        elif has_context:
            instruction = self._synthesize(task)
        else:
            instruction = ""  # 对话模式：首条指令延迟到首轮对话
        resolved_task = replace(task, instruction=instruction)

        ws = self._resolve_workspace(resolved_task)
        session = AgentSession(
            session_id=ws.workspace_id, workspace=ws, task=resolved_task
        )
        save_session(self._repo, session)
        return session

    def resume(self, session_id: str) -> AgentSession:
        """据 session_id 续跑既有会话（Req 9.5）。不存在则报错。"""
        session = load_session(self._repo, session_id)
        if session is None:
            raise InputValidationError(f"找不到可续跑的会话：{session_id}")
        return session

    # --- 内部 ---------------------------------------------------------------

    def _synthesize(self, task: WritingTask) -> str:
        """无 instruction 时据输入类型合成默认任务（Req 11.2）。"""
        if task.draft_path or task.workspace_id:
            return _DEFAULT_DRAFT_INSTRUCTION
        return _DEFAULT_TOPIC_INSTRUCTION

    def _resolve_workspace(self, task: WritingTask) -> PaperWorkspace:
        """据任务解析工作区：给了 workspace_id 则加载，否则新建。"""
        if task.workspace_id:
            ws = self._repo.load(task.workspace_id)
            if ws is None:
                raise InputValidationError(f"找不到工作区：{task.workspace_id}")
            return ws
        return self._create_workspace(task)

    def _create_workspace(self, task: WritingTask) -> PaperWorkspace:
        """据初稿/主题新建并持久化工作区（输入模式判定沿用既有语义）。

        提供初稿文件时，加载并切分为章节，直接填充大纲/章节草稿——使「启动即带
        文件」也能立刻按章节操作（与 import_draft 工具复用同一切分逻辑）。
        """
        sections = None
        draft_text = None
        if task.draft_path:
            draft_text, sections = self._load_sections(task.draft_path)

        if draft_text:
            mode = InputMode.DRAFT_REVISION
        elif task.topic_background:
            mode = InputMode.GENERATION
        else:
            # 只有 instruction、无初稿/主题：默认从零生成模式（任务自身即目标）。
            mode = InputMode.GENERATION

        ws = PaperWorkspace(
            workspace_id=uuid.uuid4().hex[:12],
            input_mode=mode,
            output_format=self._infer_output_format(task),
            original_draft=draft_text,
            topic_background=task.topic_background,
        )
        if sections:
            from paper_agent.workspace.models import OutlineNode, SectionDraft

            ws.outline = [
                OutlineNode(section_id=sid, title=title, order=i)
                for i, (sid, title, _c) in enumerate(sections)
            ]
            ws.section_drafts = {
                sid: SectionDraft(section_id=sid, title=title, content=content)
                for sid, title, content in sections
            }
            ws.draft_sections = {sid: content for sid, title, content in sections}
        if task.profile:
            ws.profile = dict(task.profile)
        if task.draft_path:
            ws.profile["input_path"] = os.path.abspath(task.draft_path)
        if task.artifact is not None:
            ws.artifact = task.artifact
        return self._repo.create(ws)

    def _load_sections(self, path: str):
        """加载初稿并切分章节；加载失败以 InputValidationError 明确报错。"""
        if self._draft_loader is not None:
            # 注入的加载器（测试用）：只给全文，单章节承载。
            text = self._draft_loader(path)
            return text, [("sec_0", "正文", text)] if text else (text, [])
        from paper_agent.agent_platform.tools.import_draft import load_sections
        from paper_agent.ingestion import DocumentLoadError

        try:
            return load_sections(path)
        except DocumentLoadError as exc:
            raise InputValidationError(f"读取初稿失败：{exc}")

    def _infer_output_format(self, task: WritingTask) -> OutputFormat:
        """据初稿扩展名推断默认输出格式；无初稿用注入默认。"""
        if task.draft_path:
            from paper_agent.entry import default_output_format

            return default_output_format(task.draft_path)
        return self._default_output


__all__ = ["TaskIntake"]
