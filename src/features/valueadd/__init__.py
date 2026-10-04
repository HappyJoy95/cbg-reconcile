# -*- coding: utf-8 -*-
"""增值（防护膜 / 附件经营）—— 一级功能模块。

```
增值（一级菜单）
├── film/            防护膜达成情况：逐店新机·达成·跟机率·毛利·礼包
├── benefit/         无忧会员权益：门店/区域/人员达成 + 后返 + 赛道奖金
└── settings/        设置：timer 注册（什么时候自动跑）
```

⚠ 口径见各子模块 `metric.py`（**开发逆推稿不进正式包**）。
⚠ 跟月度生意计划**两条口径各自独立**，别合并。
"""

from __future__ import annotations

from . import benefit, film, settings
from ..registry import Feature

#: `order=35` 排在「库存盘点」(30) 之后。
FEATURE = Feature(
    audience=("erp", "platform"), data="authorized", ops={"view": ("store", "manager", "platform")},
    key="valueadd", label="增值", order=35, types="",
    children=[
        film.SUB,
        benefit.SUB,
        settings.SUB,
    ],
)

# 路由归属只在业务登记；新增接口未登记时公共门禁拒绝。
FEATURE.routes = (
    ('GET', '/api/film', 'film', 'view'),
    ('GET', '/api/film/drill', 'film', 'view'),
    ('POST', '/api/film/export', 'film', 'export'),
    ('GET', '/api/benefit', 'benefit', 'view'),
    ('GET', '/api/benefit/drill', 'benefit', 'view'),
    ('POST', '/api/benefit/export', 'benefit', 'export'),
)
