# Implementation Plan

## Overview

在既有 Preservation_Check 之后追加一道**建议性**视觉验收闸：docx 产物后台渲染成逐页图 → 只把变化页交多模态子智能体判断版面是否符合用户诉求 → 不满足则有界重改 → 达上限诚实上报。全部默认关闭、处处优雅降级、不碰正确性核心。任务按"前置通路 → 渲染/选页 → 判断 → 编排闭环 → 接入"顺序推进，每步带测试，最后全量回归。

## Tasks

- [x] 1. 多模态消息通路（前置条件）
  - 在 `src/paper_agent/providers/llm/base.py` 新增 `ImageInput` dataclass 与 `Message.images: list[ImageInput] | None = None`（默认 None）。
  - 多模态 provider（复用/薄封装 `providers/llm/openai_compatible.py` 的 vision 变体）构造请求时：`msg.images` 非空则把本地 PNG 编码为 OpenAI 风格 `content parts`（`image_url` + `data:` base64）；`images=None` 时序列化路径与现状完全一致。
  - `providers/factory.py` 增加按 `Config.vlm_*` 装配多模态 provider 的分支（`build_vlm_provider`）；未配置返回 `None`。
  - _Requirements: 2.1, 2.2, 2.3, 2.4_

- [x] 1.1 多模态向后兼容属性测试
  - 断言任意既有纯文本 `Message`（`images=None`）序列化结果与现状逐字节一致（Property 1）。
  - _Requirements: 2.3_

- [x] 2. 渲染后端（docx → pdf）
  - 新增 `src/paper_agent/export/doc_render.py`：`RenderBackend` 协议 + `WordComBackend`（win32com、`Visible=False`、`ExportAsFixedFormat`）+ `LibreOfficeBackend`（`soffice --headless --convert-to pdf`，路径经 `PAPER_SOFFICE_PATH`/PATH 定位）+ `select_render_backend()`。
  - 每个后端 `available()` 惰性探测；`render()` 内部异常一律吞并返回 `False`（不抛）。后端带 `fidelity_note`（Word 空 / LibreOffice 为差异告警）。
  - _Requirements: 1.1, 1.2, 1.3, 6.1_

- [x] 2.1 后端优先级 + 降级测试
  - 用 fake available 断言 Word>LibreOffice>None 的选择优先级（Property 3）；`render` 异常返回 False 不抛。
  - _Requirements: 1.2, 1.3, 6.1_

- [x] 3. 逐页栅格化器
  - 在 `doc_render.py` 增 `rasterize_pdf(pdf, out_dir, *, dpi) -> list[str]`：惰性 `import fitz`，逐页转 PNG 按页序返回；PyMuPDF 不可用或失败返回 `[]`。另增 `render_docx_to_images` 一步到位封装。
  - _Requirements: 1.1, 1.4, 1.5, 6.3_

- [x] 3.1 栅格化失败降级测试
  - PyMuPDF 缺失/坏 PDF → 返回 `[]`（触发上层 skip），不抛。
  - _Requirements: 6.3_

- [x] 4. 变化页选择器
  - 新增 `src/paper_agent/agent_platform/visual/page_select.py`：`select_pages_to_judge(before_images, after_images, *, max_pages, neighbor=1) -> (pages, sampled)`。
  - 有 before：逐页图像 SHA-256 判等，不等即变化页 + 邻页上下文；无 before：前 `max_pages` 页；变化页超上限 → 截断、`sampled=True`；前后全同 → 给最小样本。
  - _Requirements: 1.7, 1.8, 1.9_

- [x] 4.1 变化页选择属性测试
  - 有基线：仅变化页(+邻页)入选；无基线：回退页面预算；送出页数恒 ≤ max_pages（Property 12）。
  - _Requirements: 1.7, 1.8, 1.9_

- [x] 5. 视觉判断子智能体
  - 新增 `src/paper_agent/agent_platform/visual/judge.py`：`VisualVerdict` dataclass + `VisualJudge.judge(page_images, layout_requirement) -> VisualVerdict`，以独立上下文 + 多模态 LLM 单轮隔离判断（SubAgentRunner.converse 为纯文本通路、不套用）。
  - 系统提示限定**粗粒度**版面判断、固定 JSON 输出；防御式解析（复用 `utils/json_parse`），坏 JSON / 调用异常 → `parsed=False`（不可信）；只读、不持写工具。
  - _Requirements: 3.1, 3.2, 3.3, 3.4_

- [x] 5.1 视觉判断解析测试
  - mock 多模态 LLM：合法 JSON → 正确 `VisualVerdict`；坏 JSON / 异常 / 空页 → `parsed=False`。
  - _Requirements: 3.2_

- [x] 6. 确定性版面触发判定
  - 新增 `src/paper_agent/agent_platform/visual/triggers.py`：`touched_layout(transcript_tail) -> bool`，依据本轮工具名（`convert_document`/`set_typesetting`/`run_python` 等）与产物 `notes`（"已设为双栏"/"宽表已跨双栏"/"图跨栏"/"已套用排版规格"）**确定性**判定，不调 LLM。
  - _Requirements: 11.1, 11.2, 11.5_

- [x] 6.1 触发判定测试
  - 含版面操作/notes → True；纯语言润色/加引用 → False（Property 4/5）。
  - _Requirements: 11.1, 11.2, 11.5_

- [x] 7. 视觉验收闸编排 + 有界重编辑循环
  - 新增 `src/paper_agent/agent_platform/visual/gate.py`：`VisualAcceptanceOutcome` dataclass + `VisualAcceptanceGate.evaluate(docx, layout_requirement, *, baseline_docx, heal_fn, max_rounds, dpi)`。
  - 编排：选后端→（无后端/未配 VLM→skip）→ 循环 render(after)+baseline diff 选页→judge→satisfied 放行 / 未满足且未达上限则 `heal_fn(defects)` 重改（baseline 置为本轮产物）→达上限诚实收尾。
  - 每处失败隔离为 skip（记 `skip_reason`）；`parsed=False` 视为不可信不驱动重改；LibreOffice 结论附 `fidelity_note`；超上限置 `sampled`。
  - _Requirements: 4.1, 4.2, 4.3, 4.4, 4.5, 5.1, 5.2, 6.2, 6.3, 7.1, 7.2, 7.3_

- [x] 7.1 闭环属性测试（mock VLM + mock 渲染）
  - 满足→一轮放行；不满足→触发 heal_fn 且重评；`heal_fn` 调用次数 ≤ max_rounds（Property 6）；达上限未满足→交付最后一版+列缺陷、不阻断（Property 7）、不标成功（Property 8）。
  - 无后端/未配 VLM/渲染或视觉异常 → skip，既有行为不变、不抛（Property 9）。
  - _Requirements: 4.1, 4.2, 4.3, 4.4, 5.1, 5.2, 6.1, 6.2, 6.3, 7.1, 7.2_

- [x] 8. Config 开关与参数
  - `Config` 新增：`visual_acceptance_enabled=False`、`vlm_provider/vlm_model/vlm_base_url/vlm_api_key_env`、`visual_acceptance_max_rounds=1`、`visual_render_dpi=150`、`visual_max_pages=6`、`soffice_path`。
  - `validate()` 对 rounds/dpi/max_pages 做范围校验与 clamp（沿用既有惯例）。
  - _Requirements: 8.1, 8.2, 8.4, 8.6_

- [x] 9. 接入 ChatController + check_layout 工具
  - ChatController `_maybe_visual_accept`（确定性触发 + agent 主动请求识别）+ app 装配（`build_agent_app` 构造 vlm、按 Config 注入 gate）+ `tools/check_layout_tool.py`（仅启用时注册的窄工具，只登记请求、实际校验由收尾统一编排）。
  - _Requirements: 8.3, 9.1, 9.2, 9.3, 11.1, 11.3, 11.4_
  - 在 `agent_platform/chat.py` 收尾（`_maybe_run_acceptance` 之后）加 `_maybe_visual_accept(user_text, new_entries)`：主开关关→直接返回；`touched_layout(new_entries)` 或本轮调过 `check_layout` → 触发 `VisualAcceptanceGate.evaluate(...)`，`layout_requirement` 取本轮用户消息、`baseline_docx` 取编辑前原 docx。
  - 结果并入回复：满足→✓（区分"视觉建议"vs"确定性验收"）；未满足→列缺陷 + LibreOffice 保真告警；skip→安静/一行说明。`session.record("visual_acceptance", ...)`。
  - 仅主开关开时注册 agent 可主动调的窄工具 `check_layout`；确定性触发独立于该工具（agent 不能靠不调它跳过校验）。
  - 装配（`agent_platform/app.py`/CLI `scripts/chat.py`）：按 Config 构造多模态 provider 与 gate 注入 controller；默认关。
  - _Requirements: 8.3, 9.1, 9.2, 9.3, 11.1, 11.3, 11.4_

- [x] 9.1 接入集成测试
  - 主开关关→零渲染/零视觉调用、回复与现状一致（Property 2）；开+版面操作→触发；开+纯润色+未调工具→不触发（Property 5）；只读不改 `section_drafts`/`verified_references`（Property 10）。
  - _Requirements: 8.3, 9.1, 11.1, 11.2, 11.3, 11.4_

- [x] 10. 真机 roundtrip 冒烟（可选，有后端才跑）
  - `@pytest.mark.roundtrip`（已在 pyproject 注册）：小 docx → `select_render_backend().render` → `rasterize_pdf` → 断言逐页 PNG 存在；无 Word/LibreOffice 或无 PyMuPDF 自动 skip。另附 check_layout 工具测试 + atomic_finalize 瞬时锁重试测试。
  - _Requirements: 1.1, 1.5_

- [x] 11. 全量回归 + 文档
  - 全套 `pytest` 通过（含新增 40+ 视觉测试）；顺带根治了 `atomic_finalize` 的 Windows 瞬时文件锁 flake（有界重试）。`.env.example` 补充 `PAPER_VLM_*` / `PAPER_VISUAL_*` 说明与"默认关、依赖缺失即优雅降级、LibreOffice 保真告警、数据外泄须知"。
  - _Requirements: 6.4, 8.1, 10.1, 10.2_

## Task Dependency Graph

```json
{
  "waves": [
    { "wave": 1, "tasks": ["1", "2", "6", "8"], "rationale": "彼此独立的地基：多模态通路、渲染后端、确定性触发判定、Config——可并行。" },
    { "wave": 2, "tasks": ["1.1", "2.1", "3", "5"], "rationale": "3 栅格化依赖 2 的后端产物；5 视觉判断依赖 1 的多模态通路；1.1/2.1 为前一波的测试。" },
    { "wave": 3, "tasks": ["3.1", "4", "5.1", "6.1", "10"], "rationale": "4 变化页选择依赖 3 栅格化；5.1/6.1/3.1 测试；10 真机 roundtrip 依赖 2/3。" },
    { "wave": 4, "tasks": ["4.1", "7"], "rationale": "7 验收闸编排汇聚 4/5/6/8；4.1 为选页测试。" },
    { "wave": 5, "tasks": ["7.1", "9"], "rationale": "9 接入 chat 依赖 7 编排闭环；7.1 为闭环属性测试。" },
    { "wave": 6, "tasks": ["9.1", "11"], "rationale": "9.1 接入集成测试；11 全量回归 + 文档收尾。" }
  ]
}
```

```
1 (多模态通路) ──┐
                 ├─> 5 (视觉判断) ──┐
2 (渲染后端) ────┤                  │
   └─> 3 (栅格化) ─> 4 (变化页选择) ─┤
6 (触发判定) ─────────────────────── ┼─> 7 (验收闸编排+闭环) ─> 9 (接入 chat) ─> 11 (回归+文档)
8 (Config) ──────────────────────── ┘         │
                                              └─> (7.1 闭环测试)
3 ─> 10 (真机 roundtrip，独立可选)

单元/属性测试子任务紧随各自父任务：1.1→1，2.1→2，3.1→3，4.1→4，5.1→5，6.1→6，7.1→7，9.1→9。
```

## Notes

- **顺序**：先做前置（1 多模态通路、2/3 渲染栅格化、8 Config），再做选页/判断/触发（4/5/6），汇入编排闭环（7），最后接入（9）与回归（11）。10 可随时做（依赖 2/3）。
- **默认关 + 优雅降级**是贯穿约束：任一依赖缺失（无 Word/LibreOffice、无 PyMuPDF、未配多模态）都 skip、回退既有行为，绝不抛到主流程。
- **测试策略**：全程 mock 多模态 LLM（不打真实 vision API，控成本/求确定）；渲染/栅格化在单元测试用 fake，只有 `@pytest.mark.roundtrip` 用真实后端且无后端自动 skip。
- **不碰正确性核心**：闸门只读产物 + 驱动重改，重改仍经既有写工具→护栏→单一写路径；不改引用/内容/忠实性逻辑。
- **Windows 环境提示**：首个 pytest 可能出现 spurious `^C`，重试即可；真机 roundtrip 需本机装 Word 或 LibreOffice + PyMuPDF。
