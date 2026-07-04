"""MCP 检索 provider：封装现成的学术检索 MCP server。

通过一个注入的「MCP 客户端」调用其工具（如 search_arxiv / fetch_paper），
把返回结果适配为统一的 ReferenceEntry。客户端以协议形式注入，
避免绑定具体 MCP SDK，便于测试与替换。
"""

from __future__ import annotations

from typing import Any, Protocol

from paper_agent.providers.retrieval.base import RetrievalError, RetrievalProvider
from paper_agent.workspace.models import ReferenceEntry


class McpClient(Protocol):
    def call_tool(self, name: str, arguments: dict) -> Any:
        ...


class McpRetrievalProvider(RetrievalProvider):
    def __init__(
        self,
        client: McpClient,
        search_tool: str = "search_papers",
        fetch_tool: str = "fetch_paper",
    ) -> None:
        self._client = client
        self._search_tool = search_tool
        self._fetch_tool = fetch_tool

    def search(self, query: str, limit: int = 10) -> list[ReferenceEntry]:
        try:
            raw = self._client.call_tool(
                self._search_tool, {"query": query, "limit": limit}
            )
        except Exception as exc:  # pragma: no cover - 取决于客户端
            raise RetrievalError(f"MCP 检索失败：{exc}") from exc
        return [self._adapt(item) for item in self._as_list(raw)]

    def fetch_metadata(self, identifier: str) -> ReferenceEntry | None:
        try:
            raw = self._client.call_tool(self._fetch_tool, {"id": identifier})
        except Exception as exc:  # pragma: no cover
            raise RetrievalError(f"MCP 元数据获取失败：{exc}") from exc
        if not raw:
            return None
        return self._adapt(raw)

    @staticmethod
    def _as_list(raw: Any) -> list[dict]:
        if isinstance(raw, dict):
            return raw.get("results") or raw.get("data") or []
        return raw or []

    @staticmethod
    def _adapt(item: dict) -> ReferenceEntry:
        source_id = item.get("doi") or item.get("arxiv_id") or item.get("id", "")
        return ReferenceEntry(
            id=str(item.get("id", source_id)),
            title=item.get("title", ""),
            authors=item.get("authors", []) or [],
            year=item.get("year"),
            source_id=str(source_id),
            source=item.get("source", "mcp"),
        )
