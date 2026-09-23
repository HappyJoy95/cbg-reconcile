"""**编号迁移**：结构改动一条一条登记，跑过的记在 `meta.schema` 里。

M15 / 阶段 3.6。以前没有这套东西：

* `dump.py` 那句 `meta.schema = "1"` **写了但全项目没人读**（实测 grep 零命中）；
* `dbmigrate.py` 是 2.1.0 的**一次性改名重建**（`MARK_REL` + `SUFFIX="bak-2.1.0"`），
  门槛靠 `BUILD.txt` 里有没有 `beta` —— 它跟"结构升级"是两码事（见 D4）。

## 四条规矩

| | |
|---|---|
| **编号** | 连续、只增不改；跑过的最大编号写进 `meta.schema` |
| **前置条件** | `need(db)` 返回 `(能不能跑, 为什么)`。**不满足 ⇒ 记成 skipped，下次再来**（不是"跳过就算完"） |
| **幂等记录** | 每跑完一条**立刻**更新 `meta.schema`（一条一提交）⇒ 中断了也知道走到哪 |
| **失败处理** | 抛 `MigrationError(哪一条、什么错)`，**不吞**；库停在上一条的编号上 |

⚠ **不负责"改库名"那类动作**：`dbmigrate` 的改名重建保留它自己的门槛
（beta 包不许动门店的库 —— 那条红线不动），这里只管**结构**。
"""

from __future__ import annotations

import datetime
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, List, Optional, Tuple

CST = datetime.timezone(datetime.timedelta(hours=8))


class MigrationError(RuntimeError):
    """迁移失败 —— 带上**是哪一条**和原始错误。不吞、不猜。"""

    def __init__(self, n: int, name: str, cause):
        super().__init__("迁移 %03d（%s）失败：%s: %s" % (n, name, type(cause).__name__, cause))
        self.n, self.name, self.cause = n, name, cause


@dataclass
class Migration:
    n: int
    name: str
    apply: Callable
    #: `(能不能跑, 为什么不能)` —— 不满足就 skipped，**下次还会再来**
    need: Callable = field(default=lambda db: (True, ""))


def _table_exists(conn, name: str) -> bool:
    row = conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
                       (name,)).fetchone()
    return row is not None


def _has_index(conn, name: str) -> bool:
    row = conn.execute("SELECT 1 FROM sqlite_master WHERE type='index' AND name=?",
                       (name,)).fetchone()
    return row is not None


# ------------------------------------------------------------------ 迁移登记表
def _m001(conn) -> None:
    """**结构基线** —— 现有那套表就是"第 1 版结构"，这里只负责把编号记上。

    ⚠ 真正的建表仍在 `dump.SCHEMA` / `pools.SCHEMA`（它们本来就是
    `CREATE TABLE IF NOT EXISTS`，谁先跑谁建）。这条迁移的**意义是那条记录**：
    从此以后"这个库是哪一版结构"有据可查（以前 `meta.schema` 写了没人读）。
    """
    conn.execute("CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT)")


def _m002(conn) -> None:
    """采集**尝试**表（M14 的 C 项要用：区分"没跑"和"跑失败了"）。

    ⚠ 加列/建表这类**向前兼容**的动作不设发布门槛（beta 包也能跑）；
    要动**已有数据**的（改名/重建/删列）才必须挂 `dbmigrate.only_in_release`。
    """
    conn.execute("""CREATE TABLE IF NOT EXISTS fetch_attempt (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        kind        TEXT NOT NULL,
        started_at  TEXT, finished_at TEXT,
        ok          INTEGER NOT NULL,
        why         TEXT,
        rows        INTEGER)""")


def _m003(conn) -> None:
    """给 `erp_sales` 的 `制单时间` 建索引。

    ⚠ 为什么值得：`app/data_state.py` 判"云商销售数据覆盖到哪天"要
    `MAX(制单时间)`，10 万行没索引实测 **12.7ms**（整个判据 29ms 的大头），
    而它每 30 秒被概览页调一次。
    """
    conn.execute('CREATE INDEX IF NOT EXISTS ix_erp_sales_made ON erp_sales("制单时间")')


MIGRATIONS: List[Migration] = [
    Migration(n=1, name="结构基线", apply=_m001),
    Migration(n=2, name="采集尝试表 fetch_attempt", apply=_m002),
    Migration(n=3, name="erp_sales 制单时间索引", apply=_m003,
              # ⚠ 表还没建（全新库 / 还没抓过云商）⇒ 这条**下次再来**，
              #   而不是"跳过就算完"（那会让索引永远建不出来）
              need=lambda db: (_table_exists(db, "erp_sales"),
                               "还没有 erp_sales 表（先跑一次抓取）")),
]


# ------------------------------------------------------------------ 读 / 写编号
def _ensure_meta(conn) -> None:
    """编号表本身是**基础设施**，不是某一条迁移的产物。

    ⚠ 踩过：`run()` 里写 `meta.schema` 时假设它已经在了 ——
    而"只跑某几条迁移"（测试、以后的部分迁移）时它并不在 ⇒
    `sqlite3.OperationalError: no such table: meta` 从**记编号**那一步炸出来，
    看起来像迁移本身失败。所以这里先确保它在。
    """
    conn.execute("CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT)")


def current(conn) -> int:
    """库现在是第几版结构。读不出来当 **0**（= 从没迁移过）。"""
    try:
        row = conn.execute("SELECT value FROM meta WHERE key='schema'").fetchone()
    except sqlite3.Error:
        return 0
    if not row:
        return 0
    try:
        return int(str(row[0]).strip())
    except (TypeError, ValueError):
        return 0


def _set_current(conn, n: int) -> None:
    conn.execute("INSERT OR REPLACE INTO meta (key, value) VALUES ('schema', ?)", (str(n),))


# ------------------------------------------------------------------ 跑
def run(conn, *, migrations: Optional[List[Migration]] = None) -> dict:
    """把 `n > 当前` 的迁移按顺序跑一遍。

    返回 `{"from", "to", "applied", "skipped"}`；失败抛 `MigrationError`。

    ⚠ **一条一提交**：中断时库里停在**上一条**的编号上，下次从那儿接着跑。
    """
    todo = sorted(migrations if migrations is not None else MIGRATIONS, key=lambda m: m.n)
    _ensure_meta(conn)
    start = current(conn)
    applied, skipped = [], []
    for m in todo:
        if m.n <= start:
            continue
        ok, why = m.need(conn)
        if not ok:
            skipped.append({"n": m.n, "name": m.name, "why": why})
            continue
        try:
            m.apply(conn)
        except Exception as e:                                 # noqa: BLE001
            conn.rollback()
            raise MigrationError(m.n, m.name, e) from e
        _set_current(conn, m.n)
        conn.commit()                                          # ⚠ 一条一提交
        applied.append({"n": m.n, "name": m.name})
    return {"from": start, "to": current(conn), "applied": applied, "skipped": skipped}


def status(conn) -> dict:
    """现在第几版、还欠几条 —— **只读**，`selftest` 和看板都调它。"""
    now = current(conn)
    out = {"schema": now, "pending": [], "skipped": []}
    for m in sorted(MIGRATIONS, key=lambda x: x.n):
        if m.n <= now:
            continue
        ok, why = m.need(conn)
        (out["pending"] if ok else out["skipped"]).append({"n": m.n, "name": m.name, "why": why})
    return out


def describe(st: dict) -> str:
    """一句话（自检/日志用）—— **文案只写这一份**。"""
    s = "结构版本 %d" % st["schema"]
    if st["pending"]:
        s += "，还欠 %d 条待跑" % len(st["pending"])
    elif st["skipped"]:
        s += "（%d 条等条件满足：%s）" % (len(st["skipped"]), st["skipped"][0]["why"])
    else:
        s += "（已是最新）"
    return s
