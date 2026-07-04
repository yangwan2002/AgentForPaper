"""文档摄入：把 txt / md / pdf / docx 加载为纯文本。"""

from paper_agent.ingestion.artifact_loader import (
    ArtifactLoadError,
    load_artifact,
)
from paper_agent.ingestion.loaders import (
    DocumentLoadError,
    load_document,
    split_draft_into_sections,
    supported_extensions,
)

__all__ = [
    "load_document",
    "supported_extensions",
    "DocumentLoadError",
    "split_draft_into_sections",
    "load_artifact",
    "ArtifactLoadError",
]
