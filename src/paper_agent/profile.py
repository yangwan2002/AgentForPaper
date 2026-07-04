"""论文档案（Paper Profile / Steering）。

借鉴 Claude Code 的 CLAUDE.md/steering 思想：把"目标期刊、写作风格、引用规范、
作者偏好"等持久化偏好固化下来，作为稳定上下文注入每次写作（也利于前缀缓存）。

来源：可由请求显式提供，或从 steering 文件加载（简单的 key: value + 自由说明）。
"""

from __future__ import annotations

from dataclasses import asdict, dataclass

# 可识别的结构化字段（其余行并入 instructions 自由说明）。
_KNOWN_KEYS = {
    "venue": "目标期刊/会议",
    "style": "格式规范",
    "language": "写作语言",
    "citation_style": "引用风格",
    "audience": "目标读者",
    "length": "篇幅要求",
}


@dataclass
class PaperProfile:
    venue: str = ""
    style: str = ""
    language: str = ""
    citation_style: str = ""
    audience: str = ""
    length: str = ""
    instructions: str = ""  # 自由文本的额外写作指引

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict | None) -> "PaperProfile":
        data = data or {}
        return cls(**{k: data.get(k, "") for k in cls.__dataclass_fields__})

    def is_empty(self) -> bool:
        return not any(getattr(self, f) for f in self.__dataclass_fields__)


def render_profile(data: dict) -> str:
    """把 profile 渲染为稳定的注入文本块；空则返回空串。"""
    profile = PaperProfile.from_dict(data)
    if profile.is_empty():
        return ""
    lines = ["[论文档案 / 写作要求]"]
    for key, label in _KNOWN_KEYS.items():
        val = getattr(profile, key)
        if val:
            lines.append(f"- {label}：{val}")
    if profile.instructions:
        lines.append(f"- 额外要求：{profile.instructions}")
    return "\n".join(lines)


def load_profile(path: str) -> PaperProfile:
    """从 steering 文件加载档案。

    格式：`key: value` 行用于已知结构化字段；以 # 开头为注释；
    其余非空行并入 instructions。
    """
    with open(path, "r", encoding="utf-8-sig") as fh:
        text = fh.read()

    fields: dict[str, str] = {}
    extra: list[str] = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if ":" in line:
            key, _, value = line.partition(":")
            key = key.strip().lower()
            if key in _KNOWN_KEYS:
                fields[key] = value.strip()
                continue
        extra.append(line)
    if extra:
        fields["instructions"] = " ".join(extra)
    return PaperProfile.from_dict(fields)
