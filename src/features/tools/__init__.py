# -*- coding: utf-8 -*-
"""小工具（一级功能模块）—— 价签 / 工牌 / 权益领取。

```
小工具（一级菜单）
├── pricetag           价签工具      ← 嵌 iframe，无定时步骤
├── badge              工牌工具      ← 同一页内切 tab
└── claim-pending      权益领取      ← 匹配销售 + 领取状态（页签名）
                          （页内「活动一览」按钮 → 悬浮窗）
```

⚠ 资源从 `~/vibe-coding/price-tag-tool` **拷进** `web/tools/price-tag/`。
⚠ **没有 `step`** —— 人手点的小工具，定时器叫不醒，也不进 `daily` 整批。
⚠ 价签/工牌 **两个 iframe 各装一格**（用户 2026-09-22：「把 iframe 拆开」）。
⚠ **串号追踪 2026-10-02 下线**（用户：「可以不要了」）——
  `sn_trace/` 目录、注册、路由、页面一并删除；children 不再按版组装。
"""

from __future__ import annotations

from ..registry import Feature, Sub
from . import claim

#: 价签 —— `data-subtab="pricetag"`，落 `#subpanel-pricetag`
PRICETAG = Sub(key="pricetag", label="价签工具", order=10)

#: 工牌 —— `data-subtab="badge"`，落 `#subpanel-badge`
BADGE = Sub(key="badge", label="工牌工具", order=20, audience=("erp", "platform"))


def build_feature() -> Feature:
    """现建这条 `Feature`（`features.build_all()` 每次都调这里）。

    ⚠ children 曾按版组装（生活馆裁 sn_trace）—— sn-trace 下线后两版一致；
      保留"现建"这个形状：以后再有按版差异不用改调用方。
    """
    return Feature(audience=("erp", "platform", "lifehall"), data="store", ops={"view": ("store", "manager", "platform")},
    key="tools", label="小工具", order=40, types="",
                   routes=(
                       ('GET', '/api/claim/activities', 'claim-pending', 'view'),
                       ('GET', '/api/claim/pending', 'claim-pending', 'view'),
                       ('POST', '/api/claim/query', 'claim-pending', 'view'),
                       ('POST', '/api/claim/status', 'claim-pending', 'modify'),
                       ('POST', '/api/claim/submit', 'claim-pending', 'enter'),
                   ),
                   children=[PRICETAG, BADGE, claim.PENDING])


#: 一级。`order=40` 排在「增值」(35) 之后。`types=""` = 三类身份都看得见。
#: ⚠ 老名字，导入时定格 —— 问"这版有哪些功能"一律走 `registry.all_features()`。
FEATURE = build_feature()
