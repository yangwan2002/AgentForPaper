"""视觉判断子智能体（visual-layout-acceptance · Task 5）。

用**多模态 LLM** 看页面图 + 用户版面诉求，产出结构化 Visual_Verdict（satisfied +
缺陷清单 + 建议）。以**独立对话上下文**、**只读**方式运行——只接收图片路径 + 诉求
文本，不持有任何写工具、不改产物（隔离即优点）。

设计取舍：既有 ``SubAgentRunner.converse`` 通路是纯文本的（无法携带图像），故此处
直接用多模态 provider 做**单轮隔离判断**（自建独立 messages），这已满足「独立上下文 +
只读」的子智能体语义；不硬套 text-only 的 converse。

判断刻意限定在**粗粒度**版面（大片空白 / 单栏 vs 双栏 / 图满栏 vs 单栏 / 表格逐字符
换行等），不做像素级精确判断。防御式解析：坏 JSON → ``parsed=False``（不可信，上层
据此不驱动重改、不卡产物）。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from paper_agent.providers.llm.base import ImageInput, LLMProvider, Message
from paper_agent.utils.json_parse import extract_json


@dataclass
class VisualVerdict:
    """视觉判断的结构化结果。"""

    satisfied: bool
    defects: list[str] = field(default_factory=list)
    advisory: str = ""
    parsed: bool = True          # 结构化解析是否成功；False=不可信（不驱动重改）


_SYSTEM = (
    "你是一个**版面**评审，只看渲染出来的论文页面图，判断版面是否符合用户诉求。"
    "只做**粗粒度**判断：是否有大片空白、正文是单栏还是双栏、图是跨双栏满宽还是挤在"
    "单栏、表格是否被逐字符换行、图/表位置是否明显不当等。**不要**做像素级精确判断，"
    "**不要**评价文字内容/学术质量。\n"
    "只输出一个 JSON 对象，格式：{\"satisfied\": true/false, "
    "\"defects\": [\"具体缺陷1\", ...], \"advisory\": \"一句话建议\"}。"
    "satisfied=true 时 defects 应为空。"
)


class VisualJudge:
    """看图判断版面是否达标（多模态、独立上下文、只读）。"""

    def __init__(self, vlm: LLMProvider) -> None:
        self._vlm = vlm

    def judge(self, page_images: list[str], layout_requirement: str) -> VisualVerdict:
        """对页面图 + 版面诉求产出 Visual_Verdict；任何异常/坏解析 → parsed=False。"""
        if not page_images:
            return VisualVerdict(satisfied=False, advisory="无可评估的页面图。", parsed=False)

        user = Message(
            role="user",
            content=(
                f"用户的版面诉求：{layout_requirement}\n"
                f"下面是渲染出的页面图（共 {len(page_images)} 张），请判断版面是否符合诉求。"
            ),
            images=[ImageInput(path=p) for p in page_images],
        )
        messages = [Message(role="system", content=_SYSTEM), user]
        try:
            resp = self._vlm.complete(messages)
        except Exception:  # noqa: BLE001 - 视觉调用失败 → 不可信，交上层降级
            return VisualVerdict(
                satisfied=False, advisory="视觉判断调用失败。", parsed=False
            )

        data = extract_json(resp.content or "")
        if not isinstance(data, dict) or "satisfied" not in data:
            return VisualVerdict(
                satisfied=False, advisory="视觉判断输出无法解析。", parsed=False
            )
        defects_raw = data.get("defects") or []
        defects = [str(d) for d in defects_raw] if isinstance(defects_raw, (list, tuple)) else []
        return VisualVerdict(
            satisfied=bool(data.get("satisfied")),
            defects=defects,
            advisory=str(data.get("advisory", "") or ""),
            parsed=True,
        )


__all__ = ["VisualVerdict", "VisualJudge"]
