"""POS 合规：`pos_metric`（纯函数口径）· `pos_report`（读库 + 推送文案）· `pos_export`（明细 Excel）。

**为什么这几个是一组**：它们只服务"POS 使用率"这一件事，
而 `cli.py` / `web.py` 只是它的入口和展示 —— 抽出来的目的是让
"新增一个功能要改多少无关模块"这个数字变小（开发目标 §4.5.5 第 1 步）。

⚠ `pos_metric` **只许 import 标准库**（纯函数、可单测，`reconcile.py` 同理）——
   这条有测试钉着（`tests/test_module_layout.py`）。
"""

from __future__ import annotations

import sys as _sys


def step_run(ctx) -> bool:
    """执行入口（注册协议 v2）—— 原 `run_daily._step_pos` 收编到这里。

    ⚠ 2026-09-19 起**直接调执行模块**（`app.pos.run`），不再手工拼 `Namespace`：
      原来那行只传了 `db`，而推送那条路第一句就是 `if not config_path: return`
      ⇒ **daily 每天算了 POS，却从来没推过 POS**。参数按名字给，漏一个测试就看得见。
    ⚠ `app.pos` 在**调用时**才 import（features → app 是延迟边，import 期不成环）；
      测试在 `src.app.pos.run` 下桩。
    """
    print("\n[3/9] POS 合规率 → out/pos-<年>.json")
    from ....app.pos import run as _pos_run
    res = _pos_run(db="", config_path=ctx.config,
                   no_push=getattr(ctx.args, "no_push", False),
                   no_mail=getattr(ctx.args, "no_mail", False), emit=ctx.emit)
    if not res.ok:
        print("\n⚠ 第 3 步（POS）没跑通：%s" % res.why, file=_sys.stderr)
        return False
    return True
