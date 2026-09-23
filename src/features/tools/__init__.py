# -*- coding: utf-8 -*-
"""小工具（一级功能模块）—— 价签 / 工牌 / 权益领取 / 串号追踪。

```
小工具（一级菜单）
├── pricetag           价签工具      ← 嵌 iframe，无定时步骤
├── badge              工牌工具      ← 同一页内切 tab
├── claim-pending      权益领取      ← 匹配销售 + 领取状态（页签名）
│                         （页内「活动一览」按钮 → 悬浮窗）
└── sn-trace           串号追踪      ← 86码/SN → 库存快照 + 销售全程
```

⚠ 资源从 `~/vibe-coding/price-tag-tool` **拷进** `web/tools/price-tag/`。
⚠ **没有 `step`** —— 人手点的小工具，定时器叫不醒，也不进 `daily` 整批。
⚠ 价签/工牌 **两个 iframe 各装一格**（用户 2026-09-22：「把 iframe 拆开」）。
"""

from __future__ import annotations

from ..registry import Feature, Sub
from . import claim, sn_trace

#: 价签 —— `data-subtab="pricetag"`，落 `#subpanel-pricetag`
PRICETAG = Sub(key="pricetag", label="价签工具", order=10)

#: 工牌 —— `data-subtab="badge"`，落 `#subpanel-badge`
BADGE = Sub(key="badge", label="工牌工具", order=20)

#: 一级。`order=40` 排在「增值」(35) 之后。`types=""` = 三类身份都看得见。
FEATURE = Feature(
    key="tools", label="小工具", order=40, types="",
    children=[PRICETAG, BADGE, claim.PENDING, sn_trace.SUB],
)
