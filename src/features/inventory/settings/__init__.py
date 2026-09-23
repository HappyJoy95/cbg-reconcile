# -*- coding: utf-8 -*-
"""库存盘点 · **设置** —— 推送开关。

⚠ 业务推不推在这里设（默认关）；通道在左下角「推送设置」。
"""

from __future__ import annotations

from ...registry import Sub

SUB = Sub(key="inventory-settings", label="设置", order=90)
