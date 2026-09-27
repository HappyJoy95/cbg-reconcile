"""**一次性注册** —— 「从现在起 N 秒后，跑一次这几步，跑完把登记删掉」。

用户 2026-09-21 定的：

> 「把 timer 的注册加上这种**单次注册**机制吧，**记录日志**，**执行完删除注册**」

## 它跟 `whens`（那张静态时刻表）是两回事

| | `when`（静态，`store.py` + `wakes.json`） | **本文件**（一次性，`wakes-once.json`） |
|---|---|---|
| 谁产生 | **功能模块声明**（写死在注册表里）+ 这台机器改的时刻 | **运行期**产生（有人点了保存 / 一句失败要重试） |
| 生命周期 | 一直在（除非删掉） | **到点跑一次就没了** |
| 页面上 | 「定时器设置」那张表 | ⚠ **不列在那儿** —— 它不是"每天都跑"的任务，混进去会让人以为要天天跑 |

⚠⚠ **为什么要有它**：在这之前，"过 5 分钟发一次"是靠 `features/store/staff.py` 里
一枚 `threading.Timer` 实现的 —— 那个东西**活在服务进程的内存里**，
服务一重启就没了；而且它跟定时器是**两套"什么时候做什么"**。
现在归到这一处：**登记落盘**（重启不丢）、**排队**（旁边有活就等下一跳，`tick` 本来就这么做）、
**走同一条执行路径**（`daily --steps …` ⇒ 运行日志/抽屉里都看得到）。

## 三条规矩（用户明说的）

1. **登记要落盘** ⇒ 服务重启不丢（`.secrets/wakes-once.json`）；
2. **都要记日志**：登记一笔、到点执行一笔、过期作废一笔（`runlog` 里 kind = `wake-once`）；
3. **执行完删掉那条登记** —— 在 `take_due()` 里**取走即删**（见那个函数的说明）。

⚠ 过期窗口：到点之后**最多等 `ONCE_GRACE_HOURS` 小时**（机器关着、服务没起）。
超过就**作废**（删掉 + 记一笔为什么），不然"关机三个月再开机"会把一堆陈旧任务全跑一遍。
"""

from __future__ import annotations

import datetime
import json
from pathlib import Path
from typing import Dict, List, Optional

from ...storage import runlog

#: 放 `.secrets/` —— 这台机器自己的东西，自更新不碰。
REL = ".secrets/wakes-once.json"

#: 到点之后还能等多久（小时）。超过就作废（见模块头最后一段）。
ONCE_GRACE_HOURS = 24

#: `runlog` 里的类型：登记 / 到点 / 作废都记在它下面。
KIND = "wake-once"

#: 一次登记的字段（写进 JSON 的就是这几样，**给人看的时间用字符串**）
FIELDS = ("key", "cmd", "at", "note", "created_at", "who")

_TIME_FMT = "%Y-%m-%d %H:%M:%S"


def path(root=None) -> Path:
    from ...paths import ROOT
    return Path(root or ROOT) / REL


def _fmt(dt: datetime.datetime) -> str:
    return dt.strftime(_TIME_FMT)


def _parse(s: str) -> Optional[datetime.datetime]:
    try:
        return datetime.datetime.strptime(str(s), _TIME_FMT)
    except (TypeError, ValueError):
        return None


def load(root=None) -> List[dict]:
    """现在登记着的一次性任务（按到点时间排，早的在前）。

    ⚠ 文件坏了 / 读不出来 ⇒ **空表**（跟 `store.load()` 一个规矩：调用方要能区分
      "没有登记"和"文件坏了"，但两者对"该不该跑"的处理是一样的 —— 都不跑）。
    """
    p = path(root)
    try:
        doc = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    items = [x for x in (doc.get("once") or []) if isinstance(x, dict) and x.get("cmd")]
    items.sort(key=lambda x: str(x.get("at") or ""))
    return items


def _save(root, items: List[dict]) -> None:
    p = path(root)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(json.dumps({"once": items}, ensure_ascii=False, indent=1),
                   encoding="utf-8")
    tmp.replace(p)


def register(root=None, cmd: str = "", *, after=None, at=None, key: str = "",
             note: str = "", who: str = "") -> dict:
    """登记一次"到点跑一遍 `cmd`"。**同一个 `key` 再登记 = 改时间**（不是新增一条）。

    | 参数 | 说明 |
    |---|---|
    | `after` | 从现在起多少**秒**（30 / 300 …） |
    | `at` | 或者给一个**绝对时刻**（`datetime` 或 `"YYYY-MM-DD HH:MM:SS"`） |
    | `key` | 合并用的键。**默认就是 `cmd`** ⇒ 同一个步骤再登记 = 把时间往后挪 |
    | `note` / `who` | 给人看的："谁在什么情况下登记的"（进日志，也进文件） |

    ⚠ `key` 默认 `cmd` 这条规矩就是"连点保存只发一封"的实现：
      第 N 次登记把第 N-1 次的**到点时间改掉**，而不是排两条。
      想让同一个步骤排两条（真要那样），显式给两个不同的 `key`。

    返回 `{ok, key, cmd, at, in, why}`；⚠ **不抛**（登记不上不该让调用方炸）。
    """
    from ...paths import ROOT
    root = root or ROOT
    try:
        now = datetime.datetime.now()
        if at is None:
            if after is None:
                raise ValueError("要么给 after（秒），要么给 at（时刻）")
            when = now + datetime.timedelta(seconds=float(after))
        elif isinstance(at, datetime.datetime):
            when = at
        else:
            when = _parse(at)
            if when is None:
                raise ValueError("at 得是 datetime 或 'YYYY-MM-DD HH:MM:SS'：%r" % (at,))
        k = str(key or cmd or "").strip()
        if not k:
            raise ValueError("key / cmd 至少要有一个")
        items = [x for x in load(root) if str(x.get("key")) != k]
        row = {"key": k, "cmd": str(cmd or ""), "at": _fmt(when),
               "note": str(note or ""), "created_at": _fmt(now), "who": str(who or "")}
        items.append(row)
        items.sort(key=lambda x: str(x.get("at") or ""))
        _save(root, items)
        secs = max(0, int(round((when - now).total_seconds())))
        runlog.record(KIND, True, note="登记：%s 在 %s（%s）"
                                       % (row["cmd"], row["at"], row["note"] or "没说为什么"),
                      detail=dict(row), root=root)
        return {"ok": True, "why": "", "key": k, "cmd": row["cmd"],
                "at": row["at"], "in": secs}
    except Exception as e:                                     # noqa: BLE001
        why = "%s: %s" % (type(e).__name__, e)
        runlog.record(KIND, False, why=why, note="登记一次性任务失败", root=root)
        return {"ok": False, "why": why, "key": str(key or cmd or ""), "cmd": str(cmd or ""),
                "at": "", "in": 0}


def cancel(root=None, key: str = "") -> bool:
    """撤掉一条登记。返回撤没撤（没这条就 False）。"""
    items = load(root)
    left = [x for x in items if str(x.get("key")) != str(key)]
    if len(left) == len(items):
        return False
    _save(root, left)
    runlog.record(KIND, True, note="撤销登记：%s" % key, root=root)
    return True


def clear(root=None) -> int:
    """全清（测试 / 排查用）。返回清掉几条。"""
    n = len(load(root))
    if n:
        _save(root, [])
    return n


def seconds_left(root=None, key: str = "") -> int:
    """还有几秒到点（0 = 没登记 / 已到点）—— 界面想知道"还要等多久"时用。"""
    now = datetime.datetime.now()
    for x in load(root):
        if key and str(x.get("key")) != str(key):
            continue
        when = _parse(x.get("at") or "")
        if when is None:
            continue
        return max(0, int(round((when - now).total_seconds())))
    return 0


def tasks(root=None, now=None) -> List[dict]:
    """给人看的一份（`at` / `等多久` / 为什么登记）—— 排查和页面都能用。"""
    now = now or datetime.datetime.now()
    out = []
    for x in load(root):
        when = _parse(x.get("at") or "")
        row = dict(x)
        row["left"] = max(0, int(round((when - now).total_seconds()))) if when else 0
        row["at_text"] = x.get("at") or ""
        out.append(row)
    return out


def peek_due(root=None, now=None) -> List[dict]:
    """只看已到点且未过期的登记；由派发方先选本次要跑的时刻组。"""
    now = now or datetime.datetime.now()
    grace = datetime.timedelta(hours=ONCE_GRACE_HOURS)
    rows = []
    for x in load(root):
        when = _parse(x.get("at") or "")
        if when is None or when > now or now - when > grace:
            continue
        row = dict(x)
        row["late"] = int((now - when).total_seconds())
        row["slot_text"] = "once:" + str(x.get("at") or "")
        rows.append(row)
    return rows


def take_due(root=None, now=None, selected=None) -> List[dict]:
    """**取走到点的那些** —— ⚠ **取走即从文件里删掉**（用户要的"执行完删除注册"）。

    为什么在"取"的时候就删，而不是等子进程跑完：
      派发出去之后我们**没法**在子进程结束时拿到回调（那一趟是独立进程，
      它收尾时写的是 `runlog` / `run_daily` 的记录）。留到"下次看到它跑完"再删，
      中间每一跳都可能重复派发 —— 而重复跑一趟上报比"没跑成"更糟。
      跟现有的 `_mark_done()`（**派发的同时就记台账**）是同一个道理：
      "派了没跑成"要人来看日志管，不是每 30 秒撞一次。
    派发失败（`spawn` 抛了）由 `tick()` 记一笔 `wake-once` 失败，**登记不复活**。

    过期（到点超过 `ONCE_GRACE_HOURS` 小时）的**不返回**，直接作废 + 记一笔为什么。

    `selected` 给派发方传本次选中的 `(key, at)`；没选中的到点登记留给下一跳。
    不传时保留原接口行为：取走全部到点登记。过期登记每次都清理。
    """
    now = now or datetime.datetime.now()
    items = load(root)
    chosen = None if selected is None else set(selected)
    grace = datetime.timedelta(hours=ONCE_GRACE_HOURS)
    due, keep, expired = [], [], []
    for x in items:
        when = _parse(x.get("at") or "")
        if when is None:
            expired.append((x, "时间读不出来（%r）" % (x.get("at"),)))
            continue
        if when > now:
            keep.append(x)
        elif (now - when) > grace:
            expired.append((x, "到点是 %s，已经过了 %d 小时（超过 %d 小时就作废）"
                            % (x.get("at"), int((now - when).total_seconds() // 3600),
                               ONCE_GRACE_HOURS)))
        else:
            if chosen is not None and (str(x.get("key") or ""),
                                       str(x.get("at") or "")) not in chosen:
                keep.append(x)
                continue
            late = int((now - when).total_seconds())
            row = dict(x)
            row["late"] = late
            row["slot_text"] = "once:" + str(x.get("at") or "")
            due.append(row)
    if expired:
        for x, why in expired:
            runlog.record(KIND, False, why=why,
                          note="作废：%s（%s）" % (x.get("cmd"), x.get("note") or "没说为什么"),
                          # ⚠ `detail` 也要带上：界面上那张"执行日志"按它显示"时间点 / 唤醒了什么"
                          #   （只写 note 的话，作废那行会显示成"（没记步骤）"）
                          detail={"key": x.get("key"), "cmd": x.get("cmd"),
                                  "at": x.get("at"), "expired": True},
                          root=root)
    if due or expired:
        _save(root, keep)
    for row in due:
        runlog.record(KIND, True,
                      # ⚠ 前缀就"到点"两个字：界面上那行标签取的是"："前面那段
                      #   （见 `timer.wakes()`），写长了那一列会被撑开
                      note="到点：%s（%s）"
                           % (row.get("cmd"), row.get("note") or "没说为什么"),
                      detail={"key": row.get("key"), "cmd": row.get("cmd"),
                              "at": row.get("at"), "late": row.get("late"),
                              "slot": row.get("slot_text")},
                      root=root)
    return due


__all__ = ["REL", "KIND", "ONCE_GRACE_HOURS", "path", "load", "register", "cancel",
           "clear", "seconds_left", "tasks", "peek_due", "take_due"]
