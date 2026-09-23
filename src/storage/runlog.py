"""运行记录（**结构化**那层）—— 谁都能写，它不依赖任何业务。

用户 2026-09-19：「加一个**系统健康状态模块**，保持自动更新和记录日志。
**每个功能模块也要给系统健康状态模块输出日志**。」

⚠ 两条分工，别混：

| | 放哪 | 用来看什么 |
|---|---|---|
| **原文日志** | `out/run.log`（启动器）/ 控制台「运行」抽屉 | **人排查**：一行一行的现场 |
| **运行记录**（本模块） | 库里的 `run_record` 表 | **机器判断**：谁跑过、成没成、多久没成功 |

## 为什么放存储层（而不是 `app/`）

它是**所有层都要调**的东西（功能、能力、入口）—— 放在 `app/` 就等于
让 `features/*` 反过来依赖上层。所以：**写接口在最底层**，
"汇总/展示"那半在 `app/health.py`（对各个来源懒加载，避免静态环）。

## 三条硬规矩

1. **绝不抛**：记不上日志不许影响正事（跟 `startup` / `record_attempt` 一个道理）；
2. **没有库就安静跳过**（刚装完还没抓过数）—— 别为了记一行去建库；
3. **一条一提交**，且只写一行（不给业务表添锁）。
"""

from __future__ import annotations

import datetime
import json
import sqlite3
import time
from pathlib import Path
from typing import Optional

from ..paths import ROOT

CST = datetime.timezone(datetime.timedelta(hours=8))

#: 留多少条（按 kind 各自保留最近 N 条，见 `prune`）
KEEP_PER_KIND = 200

DDL = """CREATE TABLE IF NOT EXISTS run_record (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    kind        TEXT NOT NULL,      -- 功能/能力的 key（pos / pools / attain / dump / update …）
    note        TEXT,               -- 一句话（给人看："周度达成"）
    started_at  TEXT, finished_at TEXT,
    ok          INTEGER NOT NULL,   -- 1 成功 / 0 失败
    why         TEXT,               -- 失败原因
    detail      TEXT)               -- 计数等，JSON"""


def find_db(root=None) -> Optional[Path]:
    """最新那个 `out/cbg-<年>.db`；没有就 `None`（**不建库**）。"""
    d = Path(root or ROOT) / "out"
    if not d.is_dir():
        return None
    cands = sorted(d.glob("cbg-[0-9][0-9][0-9][0-9].db"))
    return cands[-1] if cands else None


def ensure(conn) -> None:
    conn.execute(DDL)


def _now() -> str:
    return datetime.datetime.now(CST).strftime("%Y-%m-%d %H:%M:%S")


def record(kind: str, ok: bool, *, why: str = "", note: str = "", detail=None,
           started_at=None, finished_at=None, root=None, db="", **extra) -> None:
    """记一条（**同步、一行、绝不抛**）。

    大多数地方用 `begin()/done()` 更省事；这个函数给"只想记一笔"的场合。

    ⚠ `**extra` 会并进 `detail`（跟 `Run.done(**detail)` 一个用法）——
    这样"顺手记个行数/台数"不用先手工拼一个 dict。

    ⚠⚠ **`root` 不传 = 项目根**（门店那台机器就是它，对的）；
    但**测试必须显式传 `root=`** —— 否则会写进开发机**真库**
    （本轮 smoke test 就这么干过一次，写完当场清掉了）。
    """
    if extra:
        detail = dict(detail or {}, **extra)
    path = Path(db) if db else find_db(root)
    if not path or not path.is_file():
        return                       # ⚠ 没有库就安静跳过，别为了记日志去建库
    try:
        conn = sqlite3.connect(str(path), timeout=5)
        try:
            ensure(conn)
            conn.execute(
                "INSERT INTO run_record (kind, note, started_at, finished_at, ok, why, detail)"
                " VALUES (?,?,?,?,?,?,?)",
                (str(kind)[:80], str(note)[:200],
                 str(started_at or "") or _now(), str(finished_at or "") or _now(),
                 1 if ok else 0, str(why or "")[:800],
                 json.dumps(detail or {}, ensure_ascii=False)[:2000]))
            conn.commit()
        finally:
            conn.close()
    except Exception:                                          # noqa: BLE001
        pass                     # 记不上不许影响正事（原文日志那边还有一份）


class Run:
    """一次运行的**句柄** —— `res.done(...)` 收尾时落一行。

    ```python
    res = runlog.begin("attain", note="周度达成")
    try:
        ...
        res.done(ok=True, rows=len(rows))
    except Exception as e:
        res.done(ok=False, why=str(e)); raise
    ```
    """

    def __init__(self, kind: str, *, note="", root=None, db=""):
        self.kind, self.note = kind, note
        self._root, self._db = root, db
        self.started_at = _now()
        self._done = False

    def done(self, ok: bool = True, *, why: str = "", **detail) -> None:
        if self._done:
            return                    # 只记一次（重复调用不重复写）
        self._done = True
        record(self.kind, ok, why=why, note=self.note, detail=detail,
               started_at=self.started_at, finished_at=_now(),
               root=self._root, db=self._db)


def begin(kind: str, *, note="", root=None, db="") -> Run:
    return Run(kind, note=note, root=root, db=db)


def recent(root=None, *, limit: int = 30, kind: str = "") -> list:
    """最近几条（新的在前）—— 健康面板和 `bugreport` 用。**绝不抛**。"""
    path = find_db(root)
    if not path or not path.is_file():
        return []
    try:
        conn = sqlite3.connect("file:%s?mode=ro" % path, uri=True)
        try:
            conn.row_factory = sqlite3.Row
            sql = "SELECT * FROM run_record"
            args = []
            if kind:
                sql += " WHERE kind=?"
                args.append(kind)
            sql += " ORDER BY id DESC LIMIT ?"
            args.append(int(limit))
            out = []
            for r in conn.execute(sql, args):
                d = dict(r)
                try:
                    d["detail"] = json.loads(d.get("detail") or "{}")
                except (TypeError, ValueError):
                    d["detail"] = {}
                out.append(d)
            return out
        finally:
            conn.close()
    except sqlite3.Error:
        return []                    # 表还没建（老库）/ 库坏了 ⇒ 当作"没有记录"


def summary(root=None) -> dict:
    """按 kind 汇总：最后一次成功 / 最后一次失败 / **连着失败几次**。

    ⚠ "连着失败几次"是给**自动更新和告警**用的：一次失败可能只是网络抖，
    连着三次才值得去打扰人。
    """
    out = {}
    for r in recent(root, limit=500):        # 新的在前
        s = out.setdefault(r["kind"], {"kind": r["kind"], "last_ok": "", "last_fail": "",
                                       "last_fail_why": "", "fail_streak": 0, "runs": 0,
                                       "_seen_ok": False})
        s["runs"] += 1
        if r["ok"]:
            if not s["last_ok"]:
                s["last_ok"] = r["finished_at"]
            s["_seen_ok"] = True             # 再往前的失败就不算"连着"了
        else:
            if not s["last_fail"]:
                s["last_fail"] = r["finished_at"]
                s["last_fail_why"] = r["why"]
            if not s["_seen_ok"]:
                s["fail_streak"] += 1
    for s in out.values():
        s.pop("_seen_ok", None)
    return out


def prune(root=None, *, keep: int = KEEP_PER_KIND) -> int:
    """每个 kind 只留最近 `keep` 条（表别无限长）。返回删了几行。"""
    path = find_db(root)
    if not path or not path.is_file():
        return 0
    try:
        conn = sqlite3.connect(str(path), timeout=5)
        try:
            ensure(conn)
            n = 0
            for (kind,) in conn.execute("SELECT DISTINCT kind FROM run_record"):
                ids = [r[0] for r in conn.execute(
                    "SELECT id FROM run_record WHERE kind=? ORDER BY id DESC LIMIT -1 OFFSET ?",
                    (kind, int(keep)))]
                if ids:
                    conn.execute("DELETE FROM run_record WHERE id IN (%s)"
                                 % ",".join("?" * len(ids)), ids)
                    n += len(ids)
            conn.commit()
            return n
        finally:
            conn.close()
    except Exception:                                          # noqa: BLE001
        return 0
