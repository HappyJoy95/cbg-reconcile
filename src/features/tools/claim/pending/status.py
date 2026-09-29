# -*- coding: utf-8 -*-
"""领取状态**落盘** —— `out/claim-status.json`。

⚠ 只碰 `out/`（本机产出，自更新 NEVER_TOUCH）—— 不写代码区、不写 `.secrets`。
⚠ 写路径要先建父目录；失败**不抛给页面**（读接口兜 pending）。
"""

from __future__ import annotations

import datetime
import json
import threading
from pathlib import Path
from typing import Optional

from .....paths import ROOT

STATUS_FILE = "claim-status.json"
VALID = ("pending", "claimed", "na")

#: ⚠ **进程内互斥**（2.2.4，2026-09-29 用户：「改成两条并行吧」）。
#:
#: `set_status` 是「**整份读 → 改一条 → 整份写回**」—— 两条并行的批量提交
#: （或两个标签页同时点「标已领」）在 `ThreadingHTTPServer` 里落在**两个线程**，
#: 于是一条的写回会把另一条**整份盖掉**：表现是「领成功了却还显示待领」，
#: 而且是概率性的、事后根本查不出来 —— 所以必须在**读和写之间**上锁。
#:
#: 为什么**一把进程内锁就够**：写这个文件的只有控制台这一个进程
#: （`App.claim_status_set` / `App.claim_submit_online` 都在服务进程里），
#: CLI / 定时任务不写状态。文件本身已经用 `tmp + replace` 保原子，
#: 锁补的是"读改写"这一段，不是防半截文件。
_LOCK = threading.Lock()


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
    with _LOCK:                       # 读改写整份 JSON —— 不锁会互相盖掉（见文件头）
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
    with _LOCK:
        ok = save(root, {})
    if not ok:
        return {"ok": False, "why": "写入失败"}
    return {"ok": True, "cleared": True}
