# -*- coding: utf-8 -*-
"""领取状态**落盘** —— `out/claim-status.json`。

⚠ 只碰 `out/`（本机产出，自更新 NEVER_TOUCH）—— 不写代码区、不写 `.secrets`。
⚠ 写路径要先建父目录；失败**不抛给页面**（读接口兜 pending）。
"""

from __future__ import annotations

import datetime
import json
from pathlib import Path
from typing import Optional

from .....paths import ROOT

STATUS_FILE = "claim-status.json"
VALID = ("pending", "claimed", "na")


def path_of(root=None) -> Path:
    base = Path(root or ROOT)
    return base / "out" / STATUS_FILE


def load(root=None) -> dict:
    p = path_of(root)
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def save(root, data: dict) -> bool:
    p = path_of(root)
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(".tmp")
        tmp.write_text(
            json.dumps(data or {}, ensure_ascii=False, indent=2, sort_keys=True),
            encoding="utf-8",
        )
        tmp.replace(p)
        return True
    except OSError:
        return False


def set_status(root, key: str, status: str, by: str = "", note: str = "",
               at: Optional[str] = None) -> dict:
    """改一笔状态。返回 `{ok, why?, key, status}`。

    ⚠ 同 key 覆盖；`status` 只许 pending/claimed/na。
    """
    key = str(key or "").strip()
    if not key:
        return {"ok": False, "why": "缺少状态键"}
    status = str(status or "").strip()
    if status not in VALID:
        return {"ok": False, "why": "状态只许 %s" % "/".join(VALID)}
    data = load(root)
    data[key] = {
        "status": status,
        "at": at or datetime.datetime.now().isoformat(timespec="seconds"),
        "by": str(by or ""),
        "note": str(note or ""),
    }
    if not save(root, data):
        return {"ok": False, "why": "写入 out/%s 失败" % STATUS_FILE}
    return {"ok": True, "key": key, "status": status}


def clear_all(root) -> dict:
    """清空全部状态（设置页可选）。"""
    if not save(root, {}):
        return {"ok": False, "why": "写入失败"}
    return {"ok": True, "cleared": True}
