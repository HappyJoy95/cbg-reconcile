"""**周度目标达成情况** —— 销售数据下的第一个子模块（M2–M5）。

```
销售数据（一级）
└── attain（周度目标达成情况）← 这个
```

| 文件 | 干什么 | 能不能碰网络/库 |
|---|---|---|
| `metric.py` | **口径**：计入剔除、达成率三条、加权、逐列求和 | ❌ 纯函数（只有标准库） |
| `attain.py` | IO：读腾讯文档 → Plan、查 `erp_sales`、落盘 | ✅ |

⚠ 分两个文件不是洁癖：**口径能不能脱离网络和数据库单测**全看这条
（照 `pos_metric.py` / `pos_report.py` 的分法，那是这个项目验证过的）。
"""

from __future__ import annotations

import sys
from pathlib import Path

from .... import config_io, paths
from ....modules.timer import When
from ...registry import DEFAULT_WHENS, Step, Sub

from . import attain, metric  # noqa: F401  （对外入口：`from ..sales.attain import attain`)


def step_run(ctx) -> bool:
    """执行入口（注册协议 v2）—— 原 `run_daily._step_attain` 收编到这里。

    `ctx`：`root` / `config` / `emit` / `args`（`daily` 的解析参数，no_push 等从这拿）。
    ⚠ 读配置用 `config_io.load_raw`（读不到给 `{}`），**不要** `cli.load_config` ——
      后者找不到文件时抛 `SystemExit`（`BaseException`），会把整条 daily 带崩（坑 11）；
      本模块也**不许 import cli**（features 红线）。
    ⚠ 门店端只看自己那一行；办公室 / 平台岗看全区（`store_filter`）。
    """
    print("\n[5/9] 销售达成（本周目标 vs 云商实际）")
    store = ""
    try:
        _cfg = config_io.load_raw(Path(ctx.config)) or {}
        if not _cfg:
            raise ValueError("门店配置为空或读不到")
        # ⚠⚠ **平台岗 / 办公室不能当成"某家店"** —— 它们的 `erp_store_name`
        #   是「平台岗」（虚拟门店），拿它当过滤条件 ⇒ 一行都匹配不上 ⇒
        #   报「这家店不在目标表里（门店名对不上？）：平台岗」（实测踩过）。
        #   判据用**画像**（`show_all`），别自己去猜名字。
        #   画像的口径只有 `config_io.store_profile` 一处（`cli.store_profile_of`
        #   只是它的转发）—— 这里直接问它，root 跟 cli 一样用 `paths.ROOT`。
        _prof = config_io.store_profile(_cfg, paths.ROOT)
        # ⚠ `erp_name` 拿不到就**回落到配置里的店名**，**不许悄悄变成"看全区"** ——
        #   那正是用户 2026-09-21 报的那个 bug（门店账号看到全区）。
        store = "" if _prof.get("show_all") else (
            _prof.get("erp_name") or _cfg.get("erp_store_name") or "")
        if not _prof.get("show_all") and not store:
            raise ValueError("没有可确认的本店名称")
    except Exception as e:                                    # noqa: BLE001
        print("  ❌ 读不出本店名（%s: %s）—— 销售达成本次不算，"
              "避免把全区当本店" % (type(e).__name__, e), file=sys.stderr)
        return False
    res = attain.run(config_path=ctx.config, root=ctx.root, store_filter=store,
                     no_push=getattr(ctx.args, "no_push", False),
                     no_mail=getattr(ctx.args, "no_mail", False), emit=ctx.emit)
    if not res.get("ok"):
        print("\n⚠ 第 5 步（销售达成）没跑通：%s" % res.get("why"), file=sys.stderr)
        print("   （这一步失败**不影响**前面几步的结果，那些已经落盘了）",
              file=sys.stderr)
        return False
    return True


#: 子模块声明 —— 带 `step` ⇒ 会被**计时模块**唤醒。
#:
#: ⭐ 「**什么时候唤醒我、醒来做什么**」这两件事都写在**这儿**（用户 2026-09-20 要的接口）：
#:   `cmd="attain"` 回答"做什么"，`whens=` 回答"什么时候"。
#:   默认跟其余几步同点（21:00）⇒ 定时器上线当天的行为跟之前逐字一致；
#:   想改成"每周一 08:30"就在界面上改（`通用设置 › 定时执行`），
#:   或者把下面这行的 `DEFAULT_WHENS` 换成一个 `When(kind="weekly", weekdays=(1,), time="08:30")`。
#: ⭐ `run=step_run`：执行入口收编在这儿（协议 v2，2026-10-02）——
#:   `run_daily` 直接通过注册表里的 `Step.run` 派发 attain。
#: ⭐ 操作/数据范围声明（协议 v2，2026-10-02 收编 `_can_for` 的三把尺）：
#:   * `modify` = **只给门店**（目标拆分写 + 发送；区长/平台只读，C1 口径）；
#:   * `export` = 区长/平台（用户 2026-09-21：「区长有导出」「平台也要」「门店不用」）；
#:   * `data="authorized"` = 门店锁本店、区长锁所辖（`filter_attain_rows` 的口径）。
SUB = Sub(key="attain", label="周度目标达成情况", order=10,
          ops={"view": ("store", "manager", "platform"),
               "modify": ("store",),
               "export": ("manager", "platform")},
          data="authorized",
          step=Step(cmd="attain", label="销售达成", order=45, default=True,
                    flag="--skip-attain", whens=DEFAULT_WHENS, run=step_run))
