"""视觉版面验收（visual-layout-acceptance）：渲染 docx → 看图判断版面 → 有界重改。

组件：
- ``triggers``：确定性判定本轮是否含版面相关操作（决定是否触发视觉验收）。
- ``page_select``：前/后渲染图像比对，只挑变化页送视觉模型。
- ``judge``：多模态子智能体，看图产出结构化 Visual_Verdict。
- ``gate``：编排 + 有界重编辑循环 + 诚实上报。
"""
