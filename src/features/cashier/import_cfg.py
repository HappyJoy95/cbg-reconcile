# -*- coding: utf-8 -*-
"""玲珑导入的本机设置 —— `.secrets/cashier-import.json`（这台机器自己的）。

⚠ 放 `.secrets/`：selfupdate.NEVER_TOUCH 有它，升级不带走门店的黑名单。
⚠ 坏文件当"没设置"（回 []）—— 黑名单坏了不该让导入按钮整个报错；
   丢了顶多多导几单，人工删一下就行。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import List

from ...paths import ROOT

REL = ".secrets/cashier-import.json"


def path(root=None) -> Path:
    return Path(root if root is not None else ROOT) / REL


def load(root=None) -> List[str]:
    p = path(root)
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    if not isinstance(raw, dict):
        return []
    words = raw.get("blacklist")
    if not isinstance(words, (list, tuple)):
        return []
    out = []
    for w in words:
        w = str(w).strip()
        if w and w not in out:
            out.append(w)
    return out


def save(root=None, words=None) -> dict:
    if not isinstance(words, (list, tuple)):
        words = []
    clean = []
    for w in words:
        w = str(w).strip()
        if w and w not in clean:
            clean.append(w)
    p = path(root)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.parent / (p.name + ".tmp")
    tmp.write_text(json.dumps({"blacklist": clean}, ensure_ascii=False, indent=1),
                   encoding="utf-8")
    tmp.replace(p)
    return {"ok": True, "blacklist": clean}


__all__ = ["REL", "path", "load", "save"]
