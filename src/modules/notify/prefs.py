# -*- coding: utf-8 -*-
"""**功能推送开关** —— 「这条功能推不推」由门店自己设（模块设置页）。

用户 2026-09-22：「功能模块的设置里应该设置对应的二级标签的功能要不要推送，
比如周度重点产品这种**可推可不推的**门店可以自己设置」。

⚠ 跟「通用设置 → 邮件/企业微信」是两层：
  * 通道开不开（SMTP/webhook 有没有配）—— 电脑本身；
  * **这条业务推不推**（本文件）—— 跟业务有关，留在模块设置页。
⚠ 默认 **True**（保持旧行为：配了通道就推）；存 `.secrets/notify-prefs.json`
  （这台机器自己的选择，自更新不碰 `.secrets`）。
⚠ 发不发还要过通道自己的 `should_send` —— 这里只挡「业务上不想要这条」。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Dict, Optional

from ...paths import ROOT

#: 可推的功能（key = 后端/CLI 里用的短名）。加推送功能就在这儿登记。
FEATURES = {
    "attain": "周度目标达成",
    "plan": "月度生意计划",
    "film": "防护膜达成",
    "benefit": "无忧会员权益",
    "pos": "POS 合规",
    "pools": "双平台数据对比",
    "report": "数据上报",
    "inventory": "库存盘点",
}

#: 平台（渠道）开关 —— 用户 2026-09-22：「每个平台都加个推送开关」。
#: 键 = `notify.send` 的 `code`（mail / wecom）。
PLATFORMS = {
    "mail": "邮件",
    "wecom": "企业微信",
}

PREFS_NAME = "notify-prefs.json"


def _path(root=None) -> Path:
    root = Path(root) if root else ROOT
    return root / ".secrets" / PREFS_NAME


def _load(root=None) -> dict:
    p = _path(root)
    try:
        d = json.loads(p.read_text(encoding="utf-8"))
        return d if isinstance(d, dict) else {}
    except Exception:                                          # noqa: BLE001
        return {}


def _on(d: dict, key: str) -> bool:
    v = d.get(key)
    return True if v is None else bool(v)


def enabled(feature: str, root=None) -> bool:
    """这条功能要不要推 —— **没设过 = 开**（默认推，保持旧行为）。

    ⚠ 2026-09-23：原来写成 `bool(d.get(feature))`，缺 key / 没有文件都变成
    **关**，和本 docstring、平台开关 `_on()` 以及「配了通道就推」的旧行为相反 ——
    表现是临时 root / 新装机器上 POS 等功能被 `why_off` 判成「关掉了推送」。
    """
    if feature not in FEATURES:
        return False
    return _on(_load(root), feature)


def platform_enabled(code: str, root=None) -> bool:
    """这个平台（mail/wecom）推不推 —— 没设过 = 开。"""
    if code not in PLATFORMS:
        return True
    return _on(_load(root), "plat:" + code)


def why_off(feature: str, root=None) -> Optional[str]:
    """关着就给一句人话；开着返回 `None`。"""
    if feature not in FEATURES:
        return "不认识的功能：%s" % feature
    if enabled(feature, root):
        return None
    return "「%s」在这台机器上关掉了推送（%s › 设置）" % (FEATURES[feature], FEATURES[feature])


def block_reason(code: str, feature: str = "", root=None) -> Optional[str]:
    """`notify.send` 的**唯一闸门**（用户 2026-09-22：推不推交给 notify）。

    先看**功能**再看**平台** —— 任一关掉就不发，返回原因；都开返回 `None`。
    """
    if feature:
        why = why_off(feature, root)
        if why:
            return why
    if code in PLATFORMS and not platform_enabled(code, root):
        return "「%s」推送已关闭（设置里的平台开关）" % PLATFORMS[code]
    return None


def set_enabled(feature: str, on: bool, root=None) -> dict:
    if feature not in FEATURES:
        raise ValueError("不认识的功能：%s（只认 %s）"
                         % (feature, "、".join(sorted(FEATURES))))
    return _set(feature, on, root)


def set_platform(code: str, on: bool, root=None) -> dict:
    if code not in PLATFORMS:
        raise ValueError("不认识的平台：%s（只认 %s）"
                         % (code, "、".join(sorted(PLATFORMS))))
    return _set("plat:" + code, on, root)


def _set(key: str, on: bool, root=None) -> dict:
    d = _load(root)
    d[key] = bool(on)
    p = _path(root)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(d, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(p)
    return all_prefs(root)


def all_prefs(root=None) -> Dict[str, dict]:
    """`{key: {label, enabled, kind: feature|platform}}` —— 设置页渲染用。"""
    d = _load(root)
    out = {}
    for k, label in FEATURES.items():
        # ⚠ 与 `enabled()` 同一口径：缺 key = 默认开（`_on`），别再 `bool(d.get)` 
        out[k] = {"label": label, "enabled": _on(d, k), "kind": "feature",
                  "switched": k in d}
    for k, label in PLATFORMS.items():
        pk = "plat:" + k
        out[pk] = {"label": label + "推送", "enabled": _on(d, pk), "kind": "platform",
                   "switched": pk in d}
    return out
