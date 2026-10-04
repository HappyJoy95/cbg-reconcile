# -*- coding: utf-8 -*-
"""月度生意计划的一级功能模块（M22）。

```
月度生意计划（一级菜单）
└── monthly/   经营看板：七块 × 当月 vs 上月同期 × 销量/销售额/利润 + 环比
```

⚠ 口径在 `monthly/metric.py`（纯函数、零 IO），IO 在 `monthly/plan.py`。
⚠ 跟「周度重点产品」是**两回事**：那边算的是**目标达成率**（对目标），
  这边算的是**实际经营 + 环比**（不对目标）—— 两条口径各自独立，别合并。
"""

from __future__ import annotations

from . import monthly, settings
from ..registry import Feature

#: 一级功能模块的声明。
#:
#: ⚠ `order=15` —— **紧跟「周度重点产品」(10)**：这两个是同一族（都是"看门店卖得怎么样"），
#:   中间插进五项合规(20)/库存盘点(30)会让菜单读起来散。
#: ⚠ `types=""`：**三类门店都看**（数据范围由 `web.role_scope()` 管：门店只看自己那家）。
FEATURE = Feature(
    audience=("erp", "platform"), data="authorized", ops={"view": ("store", "manager", "platform")},
    key="plan", label="月度生意计划", order=15, types="",
    children=[
        monthly.SUB,
        settings.SUB,
    ],
)

# 路由归属只在业务登记；新增接口未登记时公共门禁拒绝。
FEATURE.routes = (
    ('GET', '/api/plan', 'monthly', 'view'),
    ('POST', '/api/plan/export', 'monthly', 'export'),
)
