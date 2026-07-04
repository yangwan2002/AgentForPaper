"""pytest / Hypothesis 全局配置。

本仓库有大量 **I/O 密集** 的属性测试（真实磁盘导出 .tex/.bib/.docx、matplotlib
出图、python-docx 读写等）。Hypothesis 默认的 **per-example 200ms wall-clock
deadline** 对这类测试并不合适：单个样例的耗时受机器负载、首次导入、文件系统抖动
影响很大，会产生 ``DeadlineExceeded`` / ``FlakyFailure`` 这类**与逻辑无关的假失败**。

因此这里注册并加载一个禁用 per-example deadline 的 Hypothesis profile（``max_examples``
等其它设置仍由各测试的 ``@settings`` 决定）。这是标准做法，且不降低测试强度——
每条属性仍跑满其声明的样例数。
"""

from __future__ import annotations

from hypothesis import settings

# 禁用 per-example 墙钟 deadline（保留各测试自定的 max_examples 等）。
settings.register_profile("paper_agent", deadline=None)
settings.load_profile("paper_agent")
