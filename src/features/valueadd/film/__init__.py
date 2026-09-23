# -*- coding: utf-8 -*-
"""**防护膜达成情况** —— 增值下的子模块。

口径见 `metric.py`（**开发逆推稿不进正式包**）。

| 文件 | 干什么 | 能不能碰库 |
|---|---|---|
| `metric.py` | **口径**：新机/达成/毛利/达成率 | ❌ 纯函数 |
| `compute.py` | IO：查 `erp_sales`（**只读 SQLite**）→ 算 → 给接口 | ✅ |
| `export.py` | 导出 Excel | ✅ |

⚠ 页面**现算**（不读缓存）；定时步骤 `film` 只负责**落一份快照**
  （`out/film.json`）方便回看 —— 算一次很快，不值得为缓存引入失效问题。
"""

from __future__ import annotations

from ...registry import Step, Sub

from . import compute, export, metric  # noqa: F401

#: 主页面。带 `step` ⇒ 点「刷新」时跟 `erp-dump` 一起跑（`REFRESH_STEPS.film`）。
#:
#: ⚠ `whens=()` = **定时器不管它**（2026-09-22 用户：「防护膜这个不用自动跑吧，
#:   跟月度生意计划一样，门店手动拉达成就行了」）—— 空 `whens` 就是"只手动"，
#:   别写成"默认 21:00"。
#: ⚠ `default=False`：也不进双击 `daily` 那趟整批。
SUB = Sub(key="film", label="防护膜达成情况", order=10,
          step=Step(cmd="film", label="防护膜达成（落快照）", order=47,
                    default=False, flag="--skip-film", whens=()))
