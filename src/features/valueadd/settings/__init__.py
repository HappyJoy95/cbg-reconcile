# -*- coding: utf-8 -*-
"""增值 · **设置** —— 只负责「什么时候自动跑」（timer 注册）。

⚠ 跟 `sales-settings` 一个角色：**二级 key 全表唯一**，所以叫 `valueadd-settings`
  不叫 `settings`。没有推送开关（防护膜这版不推送）。
"""

from __future__ import annotations

from ...registry import Sub

SUB = Sub(key="valueadd-settings", label="设置", order=90)
