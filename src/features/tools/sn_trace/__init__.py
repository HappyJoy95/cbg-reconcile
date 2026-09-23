# -*- coding: utf-8 -*-
"""串号全程追踪 —— 输入 86 码 / SN，串起库存快照与销售记录，并反查真 SN。

```
小工具 › 串号追踪（sn-trace）
  读本地 out/cbg-<年>.db · erp_stock（全部快照三列）+ erp_sales（三串号列）
  ⚠ 没有定时 Step；点刷新只保证 erp-dump 是新的
```

口径见 `trace.py`。状态不落盘。
"""

from __future__ import annotations

from ...registry import Sub

from . import trace  # noqa: F401

SUB = Sub(key="sn-trace", label="串号追踪", order=30)
