# Design Document

## Overview

本设计为 `.docx` 编辑/转换产物新增一道**视觉验收闸 + 有界重编辑循环**（Visual_Acceptance_Gate）。它在既有 Preservation_Check 之后追加一道**建议性**把关：把产物后台渲染成逐页图片，交一个用**多模态 LLM** 的子智能体「看图」判断版面是否符合用户诉求；不满足则把视觉批注反馈给编辑智能体在**有界**轮数内重改；达上限仍不满足则**如实上报**（绝不谎报）。

设计遵循既有项目契约：默认关闭、依赖缺失处处优雅降级、不触碰正确性核心、改动仍经既有写工具→护栏→单一写路径。整体分五个可独立测试的组件 + 一个编排器，串在 chat 工作流的收尾处触发。

## 与既有代码的衔接点

- **多模态通路**：扩展 `providers/llm/base.py` 的 `Message`（新增可选 `images`），既有纯文本调用逐字节不变。
- **子智能体**：复用 `agent_platform/subagents.py` 的 `SubAgentRunner`（独立上下文、共享工作区、有界）承载 Visual_Judge_SubAgent。
- **触发点**：在 `agent_platform/chat.py` 的 `_run_workflow` 与 converse 收尾处，紧跟 `_maybe_run_acceptance`（文本级验收）之后接入；确定性触发信号来自本轮 transcript 的工具/操作记录。
- **保留校验**：重编辑产物复用 `inplace_augment._preservation_check_docx`。
- **配置**：在 `Config` 新增 opt-in 开关与参数（默认关）。

## Architecture

```
┌──────────────────────────────────────────────────────────────────┐
│ ChatController 收尾（Preservation_Check + 文本验收 之后）           │
│                                                                    │
│   LayoutOperationDetector.touched(transcript)  ← 确定性判定         │
│        │  是（含 Layout_Affecting_Operation）/ 或 agent 主动调工具   │
│        ▼                                                            │
│   VisualAcceptanceGate.evaluate(docx, layout_requirement)          │
│        │                                                           │
│        ├── RenderBackend.render(docx) → pdf                        │
│        │      Word_COM_Backend (优先) │ LibreOffice_Backend (回退)  │
│        ├── PageRasterizer.rasterize(pdf, dpi) → [png,...]          │
│        ├── VisualJudge.judge(page_images, requirement)             │
│        │      └─ SubAgentRunner + Multimodal_LLM → VisualVerdict   │
│        └── 若 not satisfied 且未达上限:                             │
│               heal_fn(verdict.defects) → 编辑智能体重改 → 重新 evaluate│
│                                                                    │
│   → VisualAcceptanceOutcome（satisfied / 未满足缺陷 / 保真告警 /    │
│      跳过原因）→ 诚实并入回复                                        │
└──────────────────────────────────────────────────────────────────┘

依赖缺失（无后端 / 无多模态模型 / 渲染或视觉调用异常）→ 任一处 → 干净 skip，回退既有行为
```

## Components and Interfaces

### 1. 多模态消息通路（前置条件）

在 `providers/llm/base.py` 的 `Message` 上新增可选字段，**不破坏**既有构造：

```python
@dataclass
class ImageInput:
    """一张图像输入（本地路径或 base64）。"""
    path: str | None = None          # 本地 PNG 路径（优先）
    media_type: str = "image/png"

@dataclass
class Message:
    role: str
    content: str
    tool_calls: list[ToolCall] | None = None
    tool_call_id: str | None = None
    images: list[ImageInput] | None = None   # 新增：仅多模态调用携带；None=纯文本（行为不变）
```

- 具体多模态 provider（复用 `openai_compatible` 的 vision 变体或新增薄封装）在构造请求时，若 `msg.images` 非空则把图片编码进 OpenAI 风格的 `content parts`（`type=image_url`，本地图走 `data:` base64）。
- `images=None` 时序列化路径与现状**完全一致**（Property 向后兼容）。
- 多模态 provider 由 `Config.vlm_*` 独立配置（endpoint/model/key），与主文本 LLM 解耦；经 `providers/factory.py` 装配，未配置则返回 `None`。

### 2. RenderBackend（docx → pdf，可插拔）

```python
class RenderBackend(Protocol):
    name: str
    def available(self) -> bool: ...
    def render(self, docx_path: str, out_pdf: str) -> bool: ...   # 成功返回 True

class WordComBackend:      # Windows：win32com 不可见驱动 Word ExportAsFixedFormat
class LibreOfficeBackend:  # soffice --headless --convert-to pdf
def select_render_backend() -> RenderBackend | None:
    """Word 可用优先 Word；否则 LibreOffice；都不可用返回 None（触发 skip）。"""
```

- `WordComBackend.available()`：能 import `win32com.client` 且能创建 Word.Application（`Visible=False`）。
- `LibreOfficeBackend.available()`：能定位 `soffice`（PATH 或 `PAPER_SOFFICE_PATH`）。
- `render` 内部异常一律吞并返回 `False`（由上层 skip），绝不抛。
- 后端携带 `fidelity_note`：Word 后端为空；LibreOffice 后端为「与 Word 可能有差异」的告警文本。

### 3. PageRasterizer（pdf → 逐页 png）

```python
def rasterize_pdf(pdf_path: str, out_dir: str, *, dpi: int) -> list[str]:
    """PyMuPDF 逐页转 PNG，返回按页序的路径列表；失败返回 []（触发 skip）。"""
```

- 惰性 `import fitz`（PyMuPDF）；不可用 → 返回 `[]`。
- 栅格化**所有页**并按页序返回；「送哪些页给 VLM」由下方 `ChangedPageSelector` 决定，而非在此截断。

### 3b. ChangedPageSelector（只送真正变化的页）

docx 没有固定页码、且一处版面改动会令后文回流连带改变多页，故「改动页」只能靠**渲染后比对**得到。因编辑闸在 Preservation_Check 之后触发，我们**天然同时持有编辑前 / 后两个 docx**（就地编辑：原 docx；重编辑循环：上一轮产物），据此做前/后渲染 diff。

```python
def select_pages_to_judge(
    before_images: list[str] | None,   # 编辑前渲染的逐页 png；无基线时 None
    after_images: list[str],           # 编辑后渲染的逐页 png
    *, max_pages: int, neighbor: int = 1,
) -> tuple[list[str], bool]:
    """返回 (要送 VLM 的 after 页图片, sampled_flag)。

    - 有 before：逐页图像比对（感知哈希/像素差阈值），挑出变化页 + 前后 neighbor 页上下文。
    - 无 before：回退为前 max_pages 页预算。
    - 变化页数 > max_pages：取最相关的 max_pages 页，sampled_flag=True（裁定标注"仅采样"）。
    """
```

- 图像比对用轻量策略（同后端渲染，未变页逐像素一致 → 尺寸+快速哈希即可判等；不等即变化页）。
- **成本账**：有基线时多一次**本地渲染**（花时间，不花 API 钱），但**显著减少送进 VLM 的页数**（省真正贵的 token）且判断更聚焦，净收益为正。
- 硬上限 `max_pages` 始终作为送 VLM 页数的天花板（防大范围回流把负载撑爆）。

### 4. VisualJudge（视觉判断子智能体）

```python
@dataclass
class VisualVerdict:
    satisfied: bool
    defects: list[str]          # 具体缺陷，如 "图1上方大片空白"
    advisory: str               # 建议性说明
    parsed: bool                # 结构化解析是否成功（失败=不可信，见降级）

class VisualJudge:
    def __init__(self, vlm: LLMProvider, runner: SubAgentRunner | None = None): ...
    def judge(self, page_images: list[str], layout_requirement: str) -> VisualVerdict:
        """经 SubAgentRunner 以独立上下文 + 多模态 LLM 产出 Visual_Verdict。"""
```

- 系统提示明确：**只做粗粒度版面判断**（大片空白 / 单栏 vs 双栏 / 图满栏 vs 单栏 / 表格逐字符换行），输出固定 JSON `{satisfied, defects[], advisory}`；不做像素级判断。
- 防御式解析（复用 `utils/json_parse` 风格）：解析失败 → `parsed=False`，被上层当作「不可信、不驱动重改」处理（视觉误判不卡产物）。
- 只读：只接收图片路径 + 诉求文本，不持有任何写工具。

### 5. LayoutOperationDetector（确定性触发判定）

```python
_LAYOUT_OPS = {"convert_document", "set_typesetting", "run_python", ...}  # 版面相关
def touched_layout(transcript_tail: list[dict]) -> bool:
    """本轮 transcript 是否出现版面相关操作 / 产物 note（确定性，不调 LLM）。"""
```

- 依据本轮新增 transcript 里的工具名与 `notes`（如「已设为双栏」「宽表已跨双栏」「图跨栏」「已套用排版规格」）判定，**纯确定性**。
- 纯语言润色 / 加引用等无匹配 → `False`（不触发，省成本）。

### 6. VisualAcceptanceGate（编排 + 有界重编辑循环）

```python
@dataclass
class VisualAcceptanceOutcome:
    ran: bool                       # 是否实际执行（false=被 skip）
    satisfied: bool
    defects: list[str]
    rounds: int
    fidelity_note: str              # LibreOffice 时非空
    skip_reason: str                # ran=False 时说明为何 skip
    backend: str

class VisualAcceptanceGate:
    def evaluate(self, docx_path, layout_requirement, *, heal_fn, max_rounds, dpi) -> VisualAcceptanceOutcome
```

循环（写死、有界）：

`evaluate(docx_path, layout_requirement, *, baseline_docx=None, heal_fn, max_rounds, dpi)`：

1. `backend = select_render_backend()`；`None` → `outcome(ran=False, skip_reason="无渲染后端")`。
2. `vlm` 未配置 → `outcome(ran=False, skip_reason="未配置多模态模型")`。
3. `baseline = baseline_docx`（首轮=编辑前原 docx；后续轮=上一轮产物）。
4. for round in range(max_rounds+1)：
   a. render(after) + rasterize → 空 → `ran=False, skip_reason` 返回（故障隔离）。
   b. `before_images = render+rasterize(baseline)`（baseline 存在时；渲染失败则降级为无基线）。
   c. `pages, sampled = select_pages_to_judge(before_images, after_images, max_pages, ...)`。
   d. `verdict = judge(pages, requirement)`；`verdict.parsed=False` → 不可信，结束（不驱动重改）。
   e. `satisfied` → 结束放行。
   f. 未满足且未达上限 → `heal_fn(verdict.defects)`（编辑智能体在同一 messages 上重改，走既有写路径）；`baseline = 本轮 after`（下一轮 diff 用上一轮产物），下一轮重渲染。
5. 达上限仍未满足 → 结束，`satisfied=False` + `defects`（+ `sampled` 标注）交诚实上报。

`heal_fn` 由 chat 侧注入（`agent.converse(session, messages, 缺陷修正指令)`），因此重改**不另立写路径**。

### 7. Config 与装配

`Config` 新增（默认关 / 保守）：

```python
visual_acceptance_enabled: bool = False      # 主开关：允许用 + 成本/外泄同意
vlm_provider: str = ""                        # 多模态 provider（空=未配置）
vlm_model: str = ""
vlm_base_url: str = ""
vlm_api_key_env: str = "PAPER_VLM_API_KEY"
visual_acceptance_max_rounds: int = 1         # 有界重改轮数
visual_render_dpi: int = 150
visual_max_pages: int = 6         # 送 VLM 页数天花板（变化页优先；无基线时的页面预算）
```

`validate()` 对轮数/DPI/页数做范围校验并 clamp（与既有惯例一致）。

## 触发与集成（chat 侧）

在 `ChatController._run_workflow` 与 `_send_traced` 的收尾（`_maybe_run_acceptance` 之后）加一步 `_maybe_visual_accept(user_text, new_entries)`：

- 主开关关 → 直接返回（零渲染、零视觉调用）。
- `touched_layout(new_entries)` 为真 **或** 本轮 agent 调过 `check_layout` 工具 → 触发 `VisualAcceptanceGate.evaluate(...)`，`layout_requirement` 取本轮用户消息（版面诉求原文）。
- 结果并入回复：满足→一行 ✓（并区分「视觉建议」vs「确定性验收」）；未满足→列缺陷 + LibreOffice 保真告警；skip→（安静或一行说明）。

另暴露一个 agent 可主动调的窄工具 `check_layout`（仅在主开关开时注册），让 agent 在自认为需要时请求一次评估。**确定性触发独立于该工具**——agent 不能靠不调它来跳过对自己版面改动的校验。

## Data Models

- `ImageInput` / `Message.images`：多模态输入。
- `VisualVerdict`：视觉裁定（satisfied/defects/advisory/parsed）。
- `VisualAcceptanceOutcome`：闸门结论（ran/satisfied/defects/rounds/fidelity_note/skip_reason/backend）。
- 均为纯 dataclass，可 JSON 序列化并写入 `session.record("visual_acceptance", ...)` 供审计。

## Error Handling

| 缺失/失败 | 行为 |
|---|---|
| 主开关关 | 不执行任何渲染/视觉调用（Req 8.3） |
| 无 Word 且无 LibreOffice | skip，`skip_reason=无渲染后端`（Req 6.1） |
| 未配置多模态模型 | skip（Req 6.2, 2.4） |
| 渲染/栅格化/视觉调用抛异常或空结果 | 隔离该失败、skip、记原因（Req 6.3） |
| 视觉解析失败（parsed=False） | 视为不可信，不驱动重改、不卡产物（Req 5.1） |
| 达上限仍未满足 | 交付最后一版 + 诚实列缺陷，不阻断（Req 5.2, 7.1/7.2） |

所有 skip 都保持既有 Preservation_Check / 文本验收行为不变（Req 6.4），绝不抛到主流程。

## Testing Strategy

- **单元**：`ImageInput`/`Message.images` 向后兼容（`images=None` 序列化不变）；`select_render_backend` 优先级（Word>LibreOffice>None，用 fake available）；`rasterize_pdf` 失败返回 `[]`；`touched_layout` 对版面操作/纯润色的确定性判定；`VisualJudge` 防御式解析（坏 JSON→parsed=False）。
- **闭环（mock VLM + mock 渲染）**：满足→一轮放行；不满足→触发 heal_fn→重评；达上限→未满足诚实上报；每处依赖缺失→skip 且既有行为不变。
- **真机 roundtrip（可选、有 Word/LibreOffice 才跑）**：小 docx→渲染→逐页 png 存在且页序正确（`@pytest.mark.roundtrip`，无后端自动 skip）。
- 全程 mock VLM，不打真实多模态 API（成本/确定性）。

## Correctness Properties

### Property 1: 多模态向后兼容
任意既有纯文本 `Message`（`images=None`）经 provider 序列化的结果与现状逐字节一致。
**Validates: Requirements 2.3**

### Property 2: 主开关关等于零副作用
主开关关闭时，收尾不产生任何渲染/视觉调用，管线行为与现状一致。
**Validates: Requirements 8.1, 8.3**

### Property 3: 渲染后端优先级确定
Word 可用时必选 Word；仅 LibreOffice 可用时选 LibreOffice；都不可用返回 None。
**Validates: Requirements 1.2, 1.3, 6.1**

### Property 4: 确定性触发独立于 agent
本轮含 Layout_Affecting_Operation 时，`touched_layout` 恒为真，与 agent 是否主动调工具无关。
**Validates: Requirements 11.1, 11.4, 11.5**

### Property 5: 不盲跑
主开关开但本轮无版面操作且 agent 未主动请求时，不触发渲染/视觉调用。
**Validates: Requirements 11.2**

### Property 6: 有界终止
重编辑循环调用编辑智能体的次数不超过配置的最大轮数。
**Validates: Requirements 4.2**

### Property 7: 建议性不阻断
达上限仍未满足时，仍交付最后一版产物并附缺陷说明，不阻断交付。
**Validates: Requirements 5.2, 7.1**

### Property 8: 诚实不谎报
未满足版面目标时，收尾结论不标记为成功。
**Validates: Requirements 7.2**

### Property 9: 依赖缺失优雅降级
无后端 / 无多模态 / 渲染或视觉异常任一发生时，闸门 skip 且既有 Preservation_Check / 文本验收行为不变、不抛异常。
**Validates: Requirements 6.1, 6.2, 6.3, 6.4**

### Property 10: 只读不碰正确性核心
闸门与 VisualJudge 不修改 `section_drafts` / `verified_references`；重改仅经既有写工具路径。
**Validates: Requirements 9.1, 4.5**

### Property 11: LibreOffice 附保真告警
经 LibreOffice 后端得出的结论必附保真度告警。
**Validates: Requirements 1.6, 7.3**

### Property 12: 变化页选择
存在编辑前基线时，只有前/后渲染图像发生变化的页面（含邻页上下文）被送 Visual_Judge_SubAgent；无基线时回退为受 `max_pages` 上限约束的页面预算；送出页数恒不超过 `max_pages`。
**Validates: Requirements 1.7, 1.8, 1.9**
