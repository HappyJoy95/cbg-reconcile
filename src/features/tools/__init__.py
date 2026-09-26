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
⚠ **生活馆版（2026-09-26）**：`sn_trace/` 目录被物理裁掉（读云商库，见
  `edition.PRUNE`）⇒ children 按版条件组装，否则生活馆包 import 当场炸。
"""

from __future__ import annotations

from ... import edition as _edition          # ⚠ tools 在 src/features/ 下 ⇒ edition 要三个点
from ..registry import Feature, Sub
from . import claim

#: 价签 —— `data-subtab="pricetag"`，落 `#subpanel-pricetag`
PRICETAG = Sub(key="pricetag", label="价签工具", order=10)

#: 工牌 —— `data-subtab="badge"`，落 `#subpanel-badge`
BADGE = Sub(key="badge", label="工牌工具", order=20)


def _children():
    # 生活馆包里 sn_trace/ 目录不存在（读云商库，被裁）—— 条件 import。
    if _edition.is_lifehall():
        return [PRICETAG, BADGE, claim.PENDING]
    from . import sn_trace
    return [PRICETAG, BADGE, claim.PENDING, sn_trace.SUB]


def build_feature() -> Feature:
    """现建这条 `Feature` —— children **按版**组装。

    ⚠ 别只靠 `FEATURE` 常量顶：它在模块导入那一刻定格，同一个进程里切了
    `CBG_EDITION` 之后 children 还是旧的 —— `features.build_all()` 每次都调这里。
    """
    return Feature(key="tools", label="小工具", order=40, types="",
                   children=_children())


#: 一级。`order=40` 排在「增值」(35) 之后。`types=""` = 三类身份都看得见。
#: ⚠ 老名字，导入时定格 —— 问"这版有哪些功能"一律走 `registry.all_features()`。
FEATURE = build_feature()