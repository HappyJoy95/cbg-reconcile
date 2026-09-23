# -*- coding: utf-8 -*-
"""**经营看板** —— 月度生意计划下的子模块。

| 文件 | 干什么 | 能不能碰网络/库 |
|---|---|---|
| `metric.py` | **口径**：三层映射、期间推算、环比、聚合 | ❌ 纯函数（只有标准库） |
| `plan.py` | IO：查 `erp_sales` → 算 → 落盘 `out/plan-<年>.json` | ✅ |
| `export.py` | 导出 Excel（调 `modules.notify.export_xlsx`） | ✅ |

⚠ 分文件不是洁癖：**口径能不能脱离数据库和网络单测**全看这条
（照 `pos_metric`/`pos_report`、`attain/metric`/`attain/attain` 的分法，那是验证过的）。
"""

from __future__ import annotations

from ....modules.timer import When
from ...registry import DEFAULT_WHENS, Step, Sub

from . import metric, plan  # noqa: F401  （对外入口：`from ...plan.monthly import plan`）

#: 子模块声明 —— 带 `step` ⇒ 会被**计时模块**唤醒。
#:
#: ⭐ 「**什么时候唤醒我、醒来做什么**」两件事都写在**这儿**：
#:   `cmd="plan"` 回答"做什么"，`whens=` 回答"什么时候"。
#:
#: ⚠ 时间跟其余几步同点（21:00，`DEFAULT_WHENS`），`order=46` **紧跟 `attain`(45)** ——
#:   它读的就是那两步刚写进 `erp_sales` 的数。**每天重算**（"当月至今"每天在变），
#:   所以不另设时刻：它跟"每天那趟"是一起的。
#: ⚠ `default=True`：进整批（`daily` 不点名时也跑它）。
SUB = Sub(key="monthly", label="经营看板", order=10,
          step=Step(cmd="plan", label="月度生意计划", order=46, default=True,
                    flag="--skip-plan", whens=DEFAULT_WHENS))
