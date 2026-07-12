"""文档摄入：加载、质量检查并统一切分论文文档。"""

from dataclasses import dataclass

from paper_agent.ingestion.artifact_loader import (
    ArtifactLoadError,
    load_artifact,
)
from paper_agent.ingestion.loaders import (
    DocumentLoadError,
    IngestionConfirmationRequired,
    load_document,
    load_document_with_quality,
    supported_extensions,
)
from paper_agent.ingestion.quality import (
    IngestionQualityReport,
    assess_ingestion_quality,
)
from paper_agent.ingestion.sections import (
    split_academic_sections,
    split_document_sections,
    split_draft_into_sections,
)


@dataclass(frozen=True)
class IngestedDocument:
    text: str
    sections: list[tuple[str, str, str]]
    quality: IngestionQualityReport


def ingest_document(
    path: str,
    asset_dir: str | None = None,
    *,
    allow_confirmation: bool = False,
    confirm: bool | None = None,
) -> IngestedDocument:
    """完整摄入入口，供 CLI、Agent、评测与规划流程共享。"""
    text, quality = load_document_with_quality(
        path,
        asset_dir=asset_dir,
        allow_confirmation=allow_confirmation,
        confirm=confirm,
    )
    return IngestedDocument(
        text=text,
        sections=split_document_sections(text),
        quality=quality,
    )

__all__ = [
    "load_document",
    "supported_extensions",
    "DocumentLoadError",
    "IngestionConfirmationRequired",
    "IngestedDocument",
    "IngestionQualityReport",
    "assess_ingestion_quality",
    "ingest_document",
    "load_document_with_quality",
    "split_academic_sections",
    "split_document_sections",
    "split_draft_into_sections",
    "load_artifact",
    "ArtifactLoadError",
]
