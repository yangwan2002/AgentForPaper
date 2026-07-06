# Requirements Document

## Introduction

系统当前的工具**要么是正确性核心**（改写/引用/护栏/评审/管线——必须受控）、**要么是保格式确定性
操作**（转格式/就地增补/排版——已参数化）、**要么是结构化工作区访问**（read/locate）。但对**低风险、
机械、长尾**的需求——拼接/裁剪/缩放图片、把图插进 docx 某处、给某段设悬挂缩进、合并/拆分 PDF、
从数据画统计图、批量文件整理——系统**一个工具都没有**，且"一个需求加一个窄工具"不可持续（会导致
`stitch_images` / `crop_image` / `resize_image` ... 无限增生）。

本特性新增**一个通用的受沙箱约束的代码执行工具 `run_python`**，作为"低风险长尾工具层"：让上层
智能体**写一小段 Python**（预装 Pillow / matplotlib / pandas / python-docx / PyPDF 等）在**隔离环境**
里跑，一次性覆盖这类长尾操作，而不必逐个手写窄工具。

**严格边界（本特性的核心安全契约）**：
- `run_python` **只服务低风险长尾**（图像/数据/文件/docx 微操），**绝不替代、绝不触碰**引用真伪、
  内容护栏、忠实性、保格式转换等**正确性核心**——那些仍走既有受控工具/工作流。
- 代码在**沙箱**里跑：文件系统限定在专属工作目录、默认断网、限时/限内存；产物是新文件。
- 涉及 docx 的微操**在原稿副本上做**、跑完复用既有 `Preservation_Check` 结构无损校验，
  校验不过即判失败、保留原稿。

## Glossary

- **Sandbox（沙箱）**：受限的隔离执行环境——限定文件系统可写范围、默认断网、限时/限内存、
  资源上限；即使代码出错或被恶意提示诱导，也伤不到工作目录以外。
- **Work_Dir（工作目录）**：本次执行专属的临时目录，代码的**唯一可写区**；输入文件以只读方式
  可见，产物写在此处。
- **Long_Tail_Op（长尾操作）**：低风险、机械、难以逐个枚举的操作（拼图/裁剪/缩放/画图/合并 PDF/
  docx 段落微调等）。
- **Correctness_Core（正确性核心）**：引用真伪核验、内容反幻觉护栏、忠实性审计、保格式转换/就地
  增补等**必须受控**的能力。本特性绝不经 `run_python` 触碰它们。
- **Preservation_Check**：既有的 docx 结构无损校验（`inplace_augment` / `docx_structural`）——
  段落/表格/公式/图形计数只增不减、原标题子集保留。

## Requirements

### Requirement 1: 通用受沙箱代码执行工具

**User Story:** 作为用户，我想让系统做拼图/裁剪/画图/插图这类长尾操作，而不必系统为每个需求单独造工具。

#### Acceptance Criteria

1. WHEN 上层智能体需要执行一段 Python 完成 Long_Tail_Op THE 系统 SHALL 提供 `run_python` 工具，
   接收代码字符串与可选输入文件清单，在 Sandbox 内执行并返回 stdout/stderr/退出码/产物文件列表。
2. WHEN `run_python` 执行 THE 系统 SHALL 预置常用库（Pillow / matplotlib / pandas / python-docx /
   PyPDF 等）供代码直接 import。
3. WHEN 代码产出文件 THE 系统 SHALL 只在 Work_Dir 内产出，并把产物路径如实返回。
4. WHERE 代码正常结束 THE 系统 SHALL 返回退出码 0 与截断后的 stdout/stderr（防御式截断）。

### Requirement 2: 沙箱隔离与资源约束

**User Story:** 作为用户，我不希望这段代码能删我别的文件、外泄数据或把机器跑挂。

#### Acceptance Criteria

1. WHEN 代码运行 THE 系统 SHALL 将其可写文件系统限定在 Work_Dir，不允许写 Work_Dir 之外的路径。
2. WHEN 代码运行 THE 系统 SHALL 默认**禁用网络**（除非显式配置放行白名单）。
3. WHEN 代码运行超过配置时限 THE 系统 SHALL 终止该进程并如实上报"超时"。
4. WHEN 代码占用超过配置内存/资源上限 THE 系统 SHALL 终止并上报，不拖垮宿主。
5. WHERE 输入文件需要被代码读取 THE 系统 SHALL 以复制到 Work_Dir 或只读挂载的方式提供，
   不暴露宿主其它目录。
6. IF 代码尝试越权（写 Work_Dir 外、联网被禁时联网）THEN 系统 SHALL 使该操作失败并在 stderr 体现，
   不静默放行。

### Requirement 3: 绝不触碰正确性核心

**User Story:** 作为对学术正确性负责的产品，我要求这个通用代码工具永远不能绕过引用/内容/格式的把关。

#### Acceptance Criteria

1. WHERE 任务属于 Correctness_Core（改写章节、加/核验引用、忠实性、保格式转换/就地增补）THE 系统
   SHALL 使用既有受控工具/工作流，**不**经 `run_python` 执行。
2. WHEN `run_python` 运行 THE 系统 SHALL 不向其暴露工作区的写路径（`repo`/护栏之外的落盘通道），
   即代码无法直接改工作区 `section_drafts` / `verified_references`。
3. WHEN 工具描述与系统提示引导选择 THE 系统 SHALL 明确 `run_python` 仅用于低风险长尾（图像/数据/
   文件/docx 微操），并指引正确性核心走既有工具。

### Requirement 4: docx 微操走副本 + 无损校验

**User Story:** 作为用户，我让它给某段设悬挂缩进/插图，不能顺手把我的公式或格式弄坏。

#### Acceptance Criteria

1. WHEN `run_python` 的代码需要修改一个已有 docx THE 系统 SHALL 在该 docx 的**副本**上操作、产出
   新文件，原文件字节不变。
2. WHEN docx 微操产出新文件 THE 系统 SHALL 复用 `Preservation_Check` 校验相对原 docx 的结构无损
   （计数只增不减 + 原标题子集保留）。
3. IF Preservation_Check 失败 THEN 系统 SHALL 判该次操作失败、保留原稿、不交付破坏性产物，并诚实上报。
4. WHERE 操作仅产出全新文件（不改已有 docx，如拼图、画图）THE 系统 SHALL 不强制 Preservation_Check
   （无既有结构可比）。

### Requirement 5: 诚实上报与故障隔离

**User Story:** 作为用户，代码失败时我要看到真实原因，而不是被糊弄"已完成"。

#### Acceptance Criteria

1. IF 代码抛异常/非零退出 THEN 系统 SHALL 如实返回退出码与截断后的 stderr，不谎报成功。
2. IF 沙箱不可用（依赖/环境缺失）THEN 系统 SHALL 给出可诊断错误并拒绝执行，不退化为"裸跑"无隔离。
3. WHEN `run_python` 失败 THE 系统 SHALL 不影响其它工具与会话，异常被隔离。
4. WHEN 产物或日志文本过长 THE 系统 SHALL 防御式截断（与既有工具结果截断口径一致）。

### Requirement 6: 装配开关与向后兼容

**User Story:** 作为集成方，我要能开关此能力，且不装配时系统行为不变。

#### Acceptance Criteria

1. WHERE 提供了 `run_python` 开关配置 THE 系统 SHALL 依配置决定是否注册该工具。
2. WHERE 未启用/未装配 THE 系统 SHALL 使既有工具集与行为逐字节不变。
3. WHERE 运行平台不支持所选隔离方案 THE 系统 SHALL 在装配期给出明确提示，不静默以更弱隔离运行。
