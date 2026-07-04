"""Unit tests for `PaperWorkspace` citation_faithfulness (de)serialization.

citation-faithfulness-audit task 1.4 / Req 5.4：向后兼容——旧版序列化数据缺失
`citation_faithfulness` 键时，`from_dict` 必须回落为空列表且不抛异常。
"""

from __future__ import annotations

from paper_agent.workspace.models import (
    InputMode,
    OutputFormat,
    PaperWorkspace,
)


def _make_ws() -> PaperWorkspace:
    return PaperWorkspace(
        workspace_id="ws1",
        input_mode=InputMode.GENERATION,
        output_format=OutputFormat.LATEX,
        topic_background="多智能体协作写作",
    )


def test_from_dict_missing_key_on_full_legacy_dict_defaults_to_empty_list():
    """Req 5.4：一个字段齐全的旧版 dict（所有已知键，唯独缺 citation_faithfulness）
    反序列化后 citation_faithfulness == [] 且不抛异常。"""
    data = _make_ws().to_dict()
    # 确认这是一个「字段齐全」的 dict——包含所有已知的序列化键。
    assert "citation_faithfulness" in data
    # 模拟旧版数据：移除该键。
    del data["citation_faithfulness"]

    restored = PaperWorkspace.from_dict(data)

    assert restored.citation_faithfulness == []


def test_from_dict_missing_key_on_minimal_dict_defaults_to_empty_list():
    """Req 5.4：最小 dict（仅必需键 + 时间戳，缺 citation_faithfulness）
    反序列化后 citation_faithfulness == [] 且不抛异常。"""
    full = _make_ws().to_dict()
    minimal = {
        "workspace_id": full["workspace_id"],
        "input_mode": full["input_mode"],
        "created_at": full["created_at"],
        "updated_at": full["updated_at"],
    }
    assert "citation_faithfulness" not in minimal

    restored = PaperWorkspace.from_dict(minimal)

    assert restored.citation_faithfulness == []


def test_from_dict_present_key_roundtrips_as_list_of_dicts():
    """当 citation_faithfulness 存在时，往返保持为 list[dict]。"""
    ws = _make_ws()
    findings = [
        {"section_id": "s1", "reference_id": "r1", "verdict": "supported", "detail": "ok"},
        {"section_id": "s2", "reference_id": "r2", "verdict": "unsupported", "detail": "no evidence"},
    ]
    ws.citation_faithfulness = list(findings)

    data = ws.to_dict()
    assert data["citation_faithfulness"] == findings

    restored = PaperWorkspace.from_dict(data)

    assert isinstance(restored.citation_faithfulness, list)
    assert all(isinstance(f, dict) for f in restored.citation_faithfulness)
    assert restored.citation_faithfulness == findings
