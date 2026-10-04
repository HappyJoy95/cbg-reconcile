# -*- coding: utf-8 -*-
"""收银（生活馆利润核算的录入端）—— 菜单第三个平级入口（2026-09-29）。

```
收银（一级，nav-lifehall 直连块；full 版 CSS 藏起来）
└── cashier  收银录入   ← #subpanel-cashier，无定时步骤
```

⚠ **没有 `Step`** —— 收银是人一笔一笔录的活，定时器叫不醒它（同库存盘点）。
⚠ 注册表**两版都有**（用户拍板"收银界面"是生活馆的入口形态；key 两版一致
才过得了 `roles` 的 HTML↔`PAGE_RULES` 双向对照）—— full 版只是菜单被
`body.lifehall-edition` 的 CSS 规则藏掉，接口两边都通。
⚠ 以后利润报表挂这个 Feature 下面加第二个 `Sub`（父带目录、子带内容）。
"""

from __future__ import annotations

from ..registry import Feature, Sub

#: 收银录入 —— `data-subtab="cashier"`，落 `#subpanel-cashier`
SUB = Sub(audience=("erp", "platform", "lifehall"), data="store", ops={op: ("store",) for op in ("view", "enter", "modify", "export")},
    key="cashier", label="收银录入", order=10)

FEATURE = Feature(audience=("erp", "platform", "lifehall"), key="cashier", label="收银", order=60, types="", children=[SUB])

# 路由归属只在业务登记；新增接口未登记时公共门禁拒绝。
FEATURE.routes = (
    ('GET', '/api/cashier/entries', 'cashier', 'view'),
    ('GET', '/api/cashier/lookup', 'cashier', 'view'),
    ('GET', '/api/cashier/import-settings', 'cashier', 'view'),
    ('PUT', '/api/cashier/import-settings', 'cashier', 'modify'),
    ('POST', '/api/cashier/entry-save', 'cashier', 'enter'),
    ('POST', '/api/cashier/entry-delete', 'cashier', 'modify'),
    ('POST', '/api/cashier/policy-refresh', 'cashier', 'modify'),
    ('POST', '/api/cashier/import', 'cashier', 'enter'),
    ('POST', '/api/cashier/exclude', 'cashier', 'modify'),
    ('POST', '/api/cashier/commit', 'cashier', 'enter'),
    ('POST', '/api/cashier/export', 'cashier', 'export'),
)
