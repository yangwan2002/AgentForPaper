"""Phase 0 测试：数据模型、持久化、仓储原子更新与失败回滚。"""

from __future__ import annotations

import pytest

from paper_agent.workspace.models import (
    InputMode,
    OutputFormat,
    ParseStatus,
    PaperWorkspace,
    ReferenceEntry,
    ReviewRecord,
    ScoringDimension,
    SectionDraft,
)
from paper_agent.workspace.repository import WorkspaceRepository
from paper_agent.workspace.store import InMemoryStore, JsonFileStore, PersistenceError


def _make_ws() -> PaperWorkspace:
    return PaperWorkspace(
        workspace_id="ws1",
        input_mode=InputMode.GENERATION,
        output_format=OutputFormat.LATEX,
        topic_background="多智能体协作写作",
    )


def test_roundtrip_serialization():
    ws = _make_ws()
    ws.glossary["Agent"] = "智能体"
    ws.verified_references.append(
        ReferenceEntry(
            id="r1", title="T", authors=["A"], year=2024,
            source_id="10.1/x", source="crossref", verified=True,
        )
    )
    ws.section_drafts["s1"] = SectionDraft(
        section_id="s1", title="引言", content="...", cited_reference_ids=["r1"]
    )
    ws.review_records.append(
        ReviewRecord(iteration=1, scores={ScoringDimension.LOGIC: 7.5})
    )

    restored = PaperWorkspace.from_dict(ws.to_dict())

    assert restored.input_mode is InputMode.GENERATION
    assert restored.output_format is OutputFormat.LATEX
    assert restored.glossary["Agent"] == "智能体"
    assert restored.verified_references[0].source_id == "10.1/x"
    assert restored.section_drafts["s1"].cited_reference_ids == ["r1"]
    assert restored.review_records[0].scores[ScoringDimension.LOGIC] == 7.5


def test_review_record_roundtrip_with_failed_parse_status():
    """升级 Req 1.1/1.2：含 FAILED + 非空 unparsed_reason 的记录往返后语义不变。"""
    ws = _make_ws()
    ws.review_records.append(
        ReviewRecord(
            iteration=2,
            scores={ScoringDimension.LOGIC: 6.0, ScoringDimension.NOVELTY: 5.0},
            suggestions={ScoringDimension.LOGIC: "加强论证"},
            section_feedback={"s1": "引言需补充背景"},
            parse_status=ParseStatus.FAILED,
            unparsed_reason="json_decode_error",
        )
    )

    restored = PaperWorkspace.from_dict(ws.to_dict())

    rr = restored.review_records[0]
    assert rr.iteration == 2
    assert rr.scores[ScoringDimension.LOGIC] == 6.0
    assert rr.scores[ScoringDimension.NOVELTY] == 5.0
    assert rr.suggestions[ScoringDimension.LOGIC] == "加强论证"
    assert rr.section_feedback == {"s1": "引言需补充背景"}
    assert rr.parse_status is ParseStatus.FAILED
    assert rr.unparsed_reason == "json_decode_error"


def test_review_record_roundtrip_with_mock_fallback_status():
    """升级 Req 1.1：MOCK_FALLBACK 状态经 to_dict/from_dict 后保持不变。"""
    ws = _make_ws()
    ws.review_records.append(
        ReviewRecord(iteration=1, parse_status=ParseStatus.MOCK_FALLBACK)
    )

    restored = PaperWorkspace.from_dict(ws.to_dict())

    assert restored.review_records[0].parse_status is ParseStatus.MOCK_FALLBACK
    assert restored.review_records[0].unparsed_reason == ""


def test_review_record_default_parse_status_is_parsed():
    """升级 Req 1.1：未显式设置时默认 PARSED 且 unparsed_reason 为空，往返保持。"""
    ws = _make_ws()
    ws.review_records.append(
        ReviewRecord(iteration=1, scores={ScoringDimension.LOGIC: 8.0})
    )

    restored = PaperWorkspace.from_dict(ws.to_dict())

    assert restored.review_records[0].parse_status is ParseStatus.PARSED
    assert restored.review_records[0].unparsed_reason == ""


def test_review_record_backward_compat_missing_fields_default_to_parsed():
    """升级 Req 1.2：旧版序列化数据缺失 parse_status/unparsed_reason 时
    反序列化为 PARSED / 空字符串（向后兼容）。"""
    ws = _make_ws()
    ws.review_records.append(
        ReviewRecord(iteration=3, scores={ScoringDimension.LANGUAGE: 7.0})
    )

    data = ws.to_dict()
    # 模拟旧版数据：移除新增字段。
    for rr in data["review_records"]:
        rr.pop("parse_status", None)
        rr.pop("unparsed_reason", None)

    restored = PaperWorkspace.from_dict(data)

    rr = restored.review_records[0]
    assert rr.parse_status is ParseStatus.PARSED
    assert rr.unparsed_reason == ""
    assert rr.scores[ScoringDimension.LANGUAGE] == 7.0


def test_verified_reference_ids_excludes_unverified():
    ws = _make_ws()
    ws.verified_references.append(
        ReferenceEntry(id="r1", title="T", authors=[], year=None,
                       source_id="x", verified=True)
    )
    ws.verified_references.append(
        ReferenceEntry(id="r2", title="T2", authors=[], year=None,
                       source_id="y", verified=False)
    )
    assert ws.verified_reference_ids() == {"r1"}


def test_json_file_store_roundtrip(tmp_path):
    store = JsonFileStore(str(tmp_path))
    ws = _make_ws()
    store.save(ws)
    loaded = store.load("ws1")
    assert loaded is not None
    assert loaded.topic_background == "多智能体协作写作"
    assert store.load("missing") is None


def test_repository_update_persists(tmp_path):
    repo = WorkspaceRepository(JsonFileStore(str(tmp_path)))
    ws = repo.create(_make_ws())

    repo.update(ws, lambda w: w.glossary.update({"k": "v"}))

    reloaded = repo.load("ws1")
    assert reloaded.glossary == {"k": "v"}


def test_repository_rollback_on_persistence_failure():
    """Property 3：持久化失败时回滚内存状态，内存与磁盘保持一致。"""
    store = InMemoryStore()
    repo = WorkspaceRepository(store)
    ws = repo.create(_make_ws())

    store.fail_on_save = True
    with pytest.raises(PersistenceError):
        repo.update(ws, lambda w: w.glossary.update({"bad": "change"}))

    # 内存状态已回滚，未保留失败的修改。
    assert "bad" not in ws.glossary
