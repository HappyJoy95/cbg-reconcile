# -*- coding: utf-8 -*-
"""权益领取（挂在**小工具**下）—— 华为服务产品赠送活动查询 + 待领跟踪。

```
小工具 › 权益领取（claim-pending，原「待领清单」）
  页内「活动一览」按钮 → 悬浮窗（config/benefit-claim.yaml）
  匹配 erp_sales 机型×活动期 + 人工标已领 / 在线·手动领取
```

⚠ 跟「增值 → 无忧会员权益」是两回事：那边是卖会员/Care+ 算达成返利；
  这边是买机赠 Care+/无忧大礼包，门店要**帮客户把权益领掉**。
⚠ **没有定时 Step** —— 状态落 `out/claim-status.json`（自更新不碰 out/）。
"""

from __future__ import annotations

from . import activities, pending, submit  # noqa: F401

#: 给 tools 父模块用的两个二级页
ACTIVITIES = activities.SUB
PENDING = pending.SUB
