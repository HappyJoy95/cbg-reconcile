# -*- coding: utf-8 -*-
"""待领清单 —— 按机型×活动期匹配 `erp_sales`，并跟踪领取状态。

⚠ `whens=()` = 定时器不管；**没有 step** —— 页面点刷新只走 `erp-dump`，
  匹配在读接口现算（同 film/benefit 手动思路，但不落 daily 步骤名）。
"""

from __future__ import annotations

from ....registry import Sub

from . import compute, metric, status  # noqa: F401

SUB = Sub(key="claim-pending", label="权益领取", order=20)
