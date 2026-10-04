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


def step_run(ctx) -> bool:
    """执行入口（注册协议 v2）—— 原 `run_daily._step_plan` 收编到这里。

    ⚠ 失败**不中止后面**（跟 attain 一样，`fatal` 缺省 False）：它只落自己那份
      JSON，别的步骤不读它。
    """
    import sys as _sys
    print("\n[6/9] 月度生意计划（七块 × 本月至今 vs 上月同期）")
    # ⚠ 走模块属性 `plan.run`（测试在 `src.features.plan.monthly.plan.run` 下桩）——
    #   别改成 `from .plan import run` 的名字绑定，那样打不着桩，
    #   测试会真写项目根 out/（2026-09-23 并行偶发红的根因）。
    res = plan.run(root=ctx.root, emit=ctx.emit)
    if not res.get("ok"):
        print("\n⚠ 第 6 步（月度生意计划）没跑通：%s" % res.get("why"),
              file=_sys.stderr)
        print("   （这一步失败**不影响**前面几步的结果，那些已经落盘了）",
              file=_sys.stderr)
        return False
    return True


#: 子模块声明 —— 带 `step` ⇒ 会被**计时模块**唤醒。
#:
#: ⭐ 「**什么时候唤醒我、醒来做什么**」两件事都写在**这儿**：
#:   `cmd="plan"` 回答"做什么"，`whens=` 回答"什么时候"。
#:
#: ⚠ 时间跟其余几步同点（21:00，`DEFAULT_WHENS`），`order=46` **紧跟 `attain`(45)** ——
#:   它读的就是那两步刚写进 `erp_sales` 的数。**每天重算**（"当月至今"每天在变），
#:   所以不另设时刻：它跟"每天那趟"是一起的。
#: ⚠ `default=True`：进整批（`daily` 不点名时也跑它）。
#: ⭐ `run=step_run` + 操作/数据范围（协议 v2，2026-10-02）：导出 = 区长/平台
#:   （跟达成同口径）；锁店 = `filter_plan_rows` 的 `authorized` 档。
SUB = Sub(key="monthly", label="经营看板", order=10,
          ops={"view": ("store", "manager", "platform"),
               "export": ("manager", "platform")},
          data="authorized",
          step=Step(cmd="plan", label="月度生意计划", order=46, default=True,
                    flag="--skip-plan", whens=DEFAULT_WHENS, run=step_run))
