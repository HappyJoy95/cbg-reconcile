# -*- coding: utf-8 -*-
"""**无忧会员权益（汇机保）** —— 增值下的子模块。

口径见 `metric.py`（**开发逆推稿不进正式包**）。

| 文件 | 干什么 | 能不能碰库 |
|---|---|---|
| `metric.py` | **口径**：六档/区域/赛道/人员/奖金 | ❌ 纯函数 |
| `compute.py` | IO：查 `erp_sales` → 三块 + 赛道奖 | ✅ |
| `export.py` | 导出 Excel | ✅ |

可配置表：`config/valueadd-benefit.yaml`（赛道/台量目标/区域，**不含人员**）。
"""

from __future__ import annotations

from ...registry import Step, Sub

from . import compute, export, metric  # noqa: F401

#: 主页面。带 `step` ⇒ 点「刷新」时跟 `erp-dump` 一起跑。
#:
#: ⚠ `whens=()` = 定时器不管（同防护膜：门店手动拉达成）；
#: ⚠ `default=False`：不进双击 `daily` 整批。
SUB = Sub(key="benefit", label="无忧会员权益", order=20,
          step=Step(cmd="benefit", label="无忧会员权益（落快照）", order=48,
                    default=False, flag="--skip-benefit", whens=()))
