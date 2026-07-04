# 补充说明

- 实验是在 4 卡 3090 上跑的，单卡复现需要把 batch_size 调到 8 同时 lambda_cv 调到 0.3。
- attention reweighting 模块的实现位于 `model/attention.py:CrossViewReweighter`。
- 论文里需要强调：cross-view consistency loss 在视角差 > 60° 时增益最大。
