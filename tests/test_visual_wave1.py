"""visual-layout-acceptance 波1 测试：多模态通路向后兼容 + 渲染后端优先级 + 触发判定。

覆盖 Property 1（多模态向后兼容）、Property 3（后端优先级）、Property 4/5（确定性触发）。
全程不打真实 vision API、不依赖真实 Word/LibreOffice/PyMuPDF。
"""

from __future__ import annotations

from paper_agent.agent_platform.visual.triggers import touched_layout
from paper_agent.export import doc_render
from paper_agent.export.doc_render import (
    LibreOfficeBackend,
    WordComBackend,
    select_render_backend,
)
from paper_agent.providers.llm.base import ImageInput, Message
from paper_agent.providers.llm.openai_compatible import _to_api_message


# --------------------------------------------------------------------------- #
# Property 1: 多模态向后兼容——纯文本消息序列化与现状逐字节一致
# --------------------------------------------------------------------------- #
def test_text_only_message_serialization_unchanged():
    m = Message(role="user", content="你好")
    assert _to_api_message(m) == {"role": "user", "content": "你好"}


def test_images_none_is_plain_string_content():
    m = Message(role="user", content="正文", images=None)
    api = _to_api_message(m)
    assert api["content"] == "正文"          # 仍是字符串，不是 parts 列表
    assert isinstance(api["content"], str)


def test_message_with_image_becomes_parts():
    m = Message(
        role="user", content="看这页",
        images=[ImageInput(data_url="data:image/png;base64,AAAA")],
    )
    api = _to_api_message(m)
    assert isinstance(api["content"], list)
    kinds = [p["type"] for p in api["content"]]
    assert "text" in kinds and "image_url" in kinds


def test_image_from_local_path_encoded_base64(tmp_path):
    png = tmp_path / "x.png"
    png.write_bytes(b"\x89PNG\r\n\x1a\nfakebytes")
    m = Message(role="user", content="", images=[ImageInput(path=str(png))])
    api = _to_api_message(m)
    url = api["content"][0]["image_url"]["url"]
    assert url.startswith("data:image/png;base64,")


# --------------------------------------------------------------------------- #
# Property 3: 渲染后端优先级 Word > LibreOffice > None
# --------------------------------------------------------------------------- #
def _patch_avail(monkeypatch, *, word: bool, libre: bool):
    monkeypatch.setattr(WordComBackend, "available", lambda self: word)
    monkeypatch.setattr(LibreOfficeBackend, "available", lambda self: libre)


def test_backend_prefers_word(monkeypatch):
    _patch_avail(monkeypatch, word=True, libre=True)
    assert select_render_backend().name == "word_com"


def test_backend_falls_back_to_libreoffice(monkeypatch):
    _patch_avail(monkeypatch, word=False, libre=True)
    assert select_render_backend().name == "libreoffice"


def test_backend_none_when_all_unavailable(monkeypatch):
    _patch_avail(monkeypatch, word=False, libre=False)
    assert select_render_backend() is None


def test_libreoffice_has_fidelity_note():
    assert LibreOfficeBackend().fidelity_note           # 非空
    assert WordComBackend().fidelity_note == ""          # Word 无保真差


def test_rasterize_missing_pymupdf_returns_empty(monkeypatch, tmp_path):
    # 无 PyMuPDF / 坏 PDF → 返回 []，不抛。
    bad = tmp_path / "bad.pdf"
    bad.write_bytes(b"not a real pdf")
    assert doc_render.rasterize_pdf(str(bad), str(tmp_path), dpi=100) == []


# --------------------------------------------------------------------------- #
# Property 4/5: 确定性触发判定
# --------------------------------------------------------------------------- #
def test_touched_layout_true_on_convert_tool():
    tail = [{"kind": "tool_call", "name": "convert_document"}]
    assert touched_layout(tail) is True


def test_touched_layout_true_on_run_python():
    assert touched_layout([{"name": "run_python"}]) is True


def test_touched_layout_true_on_layout_note():
    tail = [{"name": "x", "notes": ["已设为双栏。"], "files": ["out/paper.docx"]}]
    assert touched_layout(tail) is True


def test_touched_layout_false_on_pure_polish():
    tail = [
        {"name": "rewrite_section", "notes": ["润色了引言语言"]},
        {"name": "read_section"},
    ]
    assert touched_layout(tail) is False


def test_touched_layout_false_on_empty():
    assert touched_layout([]) is False
