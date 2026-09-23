"""POS 合规：`pos_metric`（纯函数口径）· `pos_report`（读库 + 推送文案）· `pos_export`（明细 Excel）。

**为什么这几个是一组**：它们只服务"POS 使用率"这一件事，
而 `cli.py` / `web.py` 只是它的入口和展示 —— 抽出来的目的是让
"新增一个功能要改多少无关模块"这个数字变小（开发目标 §4.5.5 第 1 步）。

⚠ `pos_metric` **只许 import 标准库**（纯函数、可单测，`reconcile.py` 同理）——
   这条有测试钉着（`tests/test_module_layout.py`）。
"""
