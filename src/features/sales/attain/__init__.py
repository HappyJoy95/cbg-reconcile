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

from ....modules.timer import When
from ...registry import DEFAULT_WHENS, Step, Sub

from . import attain, metric  # noqa: F401  （对外入口：`from ..sales.attain import attain`）

#: 子模块声明 —— 带 `step` ⇒ 会被**计时模块**唤醒。
#:
#: ⭐ 「**什么时候唤醒我、醒来做什么**」这两件事都写在**这儿**（用户 2026-09-20 要的接口）：
#:   `cmd="attain"` 回答"做什么"，`whens=` 回答"什么时候"。
#:   默认跟其余几步同点（21:00）⇒ 定时器上线当天的行为跟之前逐字一致；
#:   想改成"每周一 08:30"就在界面上改（`通用设置 › 定时执行`），
#:   或者把下面这行的 `DEFAULT_WHENS` 换成一个 `When(kind="weekly", weekdays=(1,), time="08:30")`。
SUB = Sub(key="attain", label="周度目标达成情况", order=10,
          step=Step(cmd="attain", label="销售达成", order=45, default=True,
                    flag="--skip-attain", whens=DEFAULT_WHENS))
