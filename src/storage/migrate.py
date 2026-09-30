"""**编号迁移**：结构改动一条一条登记，跑过的记在 `meta.schema` 里。

M15 / 阶段 3.6。以前没有这套东西：

* `dump.py` 那句 `meta.schema = "1"` **写了但全项目没人读**（实测 grep 零命中）；
* `dbmigrate.py` 是 2.1.0 的**一次性改名重建**（`MARK_REL` + `SUFFIX="bak-2.1.0"`），
  门槛靠 `BUILD.txt` 里有没有 `beta` —— 它跟"结构升级"是两码事（见 D4）。

## 四条规矩

| | |
|---|---|
| **编号** | 连续、只增不改；**已跑过的编号逐条记在 `meta.migrations`**（逗号分隔），`meta.schema` 仍是"已跑最大编号"（给显示/老代码看） |
| **前置条件** | `need(db)` 返回 `(能不能跑, 为什么)`。**不满足 ⇒ 记成 skipped，下次再来**（不是"跳过就算完"） |
| **幂等记录** | 每跑完一条**立刻**记账 + 更新编号（一条一提交）⇒ 中断了也知道走到哪 |
| **失败处理** | 抛 `MigrationError(哪一条、什么错)`，**不吞**；库停在上一条的账上 |

⚠⚠ **为什么 2026-09-29 从"按编号截断"改成"逐条记账"**（收银界面那轮）：
老实现 `if m.n <= meta.schema: continue` —— 一旦**后面的编号**跑过
（005/006 无条件建表），前面被 `need` 跳过的 003/004 就**永远不跑了**，
`meta.schema` 却显示"已到 6"。这正是本模块自己要防的
「**跳过 ≠ 做完**」：索引/表再也建不出来，而状态页一片绿。
`tests/test_migrate.py::test_条件满足之后自己就跑了` 就是这条的回归钉子
（先跑后面的、再补前面的，老实现当场红）。
老库兼容：`meta.migrations` 缺失时按 `meta.schema` 补账
（当年的语义 = 1..schema 真都跑过，两条键的写入始终同步）。

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


def _m004(conn) -> None:
    """**利润核算结果表**（生活馆，2026-09-29）—— 开发目标见
    `.dsh/docs/2026-09-29-生活馆利润核算-数据表-开发目标.md`。

    形状跟「四池」那两张同理（`comparison/store.py` 顶部那段）：

    * **只钉核心列** —— 单据/行号（挂回 `orders`/`order_lines` 的键）
      + 四个金额列（销售/成本/返利/利润）；
    * **政策与接口给的列不写死** —— pmall「价格及返利政策」导出的
      「基准提货价」「无条件单台返利金额」这类字段由 `ensure_columns`
      按返回**当场补**（项目口径：接口多给什么，库里就多一列）。
      写死在迁移里的后果是接口一改名就得动编号迁移 —— 那是白给自己挖坑。

    ⚠ 主键 `(document_no, line_no)`：利润**重算要覆盖同一行**，
      没主键就会越积越多（同一单据行算一次留一条，界面上翻一倍）。
    ⚠ 加列/建表是**向前兼容**的动作，不设发布门槛（beta 包也能跑，
      同 `_m002` 的注释）。
    ⚠ 2026-09-29 **去掉了 `orders` 前置**：收银界面（利润核算第三步）允许
      **纯手动店**建库 —— 那种机器永远不跑 dump，表等 orders 就等于永远建不出来。
      （原来挂 orders 只是为了保住"空库 applied 只有 1、2"的老钉子，那条已改。）
    """
    conn.execute("""CREATE TABLE IF NOT EXISTS profit_result (
        document_no   TEXT NOT NULL,
        line_no       INTEGER NOT NULL,
        sn            TEXT,
        sku           TEXT,
        item_name     TEXT,
        quantity      REAL,
        sale_amount   REAL,
        cost_amount   REAL,
        rebate_amount REAL,
        profit        REAL,
        calc_at       TEXT,
        source        TEXT,
        PRIMARY KEY (document_no, line_no))""")


def _m005(conn) -> None:
    """**收银流水**（收银界面 v1，2026-09-29）—— 人工录的销售事实。

    ⚠ 跟 `profit_result` **分两张表**（用户拍的字段方案里"来源=manual"就是它）：
      流水是**事实**（人工改/删），利润是**派生**（以后重算覆盖）——
      混一张表会让"改一笔流水"和"重算利润"互相踩。
    ⚠ 无前置（同 `_m004` 去前置的理由）：收银机可能不跑抓取，表要当场能建。
    """
    conn.execute("""CREATE TABLE IF NOT EXISTS sale_entries (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        sold_at     TEXT NOT NULL,
        goods_code  TEXT,
        goods_name  TEXT,
        quantity    REAL NOT NULL DEFAULT 1,
        amount      REAL NOT NULL,
        seller      TEXT,
        note        TEXT,
        source      TEXT NOT NULL DEFAULT 'manual',
        created_at  TEXT,
        updated_at  TEXT)""")
    conn.execute("CREATE INDEX IF NOT EXISTS ix_sale_entries_day ON sale_entries(sold_at)")


def _m006(conn) -> None:
    """**政策快照**（pmall 价格与返利政策，整表覆盖）。

    ⚠ 只钉键列 + `fetched_at`：政策那 9 列（`基准提货价*` 带星号那种表头）
      由 `ensure_columns` 按导出**原样**长 —— 接口改名不用动编号迁移
      （跟 `_m004` 的动态列同一口径）。
    ⚠ 不设主键去重：整表快照"先清后写"（同四池快照的道理）；
      `goods_code` 上建索引给收银界面的编码反查用。
    """
    conn.execute("""CREATE TABLE IF NOT EXISTS price_policy (
        fetched_at  TEXT NOT NULL,
        goods_code  TEXT NOT NULL)""")
    conn.execute("CREATE INDEX IF NOT EXISTS ix_price_policy_code ON price_policy(goods_code)")


def _m007(conn) -> None:
    """收银流水补列（2026-09-30 订单卡改造）—— SN / 配件 / 组合支付 / 玲珑来源。

    ⚠ `external_id` 上建唯一索引（玲珑 `document_no`）：SQLite 的 UNIQUE 索引
      **放行多个 NULL** ⇒ 手工单（external_id 空）互不冲突，玲珑单天然幂等。
    ⚠ `excluded` 不给 DEFAULT（ALTER 加列 + 非空默认在老 SQLite 上有坑）——
      读侧一律 `excluded IS NULL OR =0` 兜底（`list_entries` 负责）。
    """
    conn.execute("ALTER TABLE sale_entries ADD COLUMN sn TEXT")
    conn.execute("ALTER TABLE sale_entries ADD COLUMN accessories TEXT")
    conn.execute("ALTER TABLE sale_entries ADD COLUMN payments TEXT")
    conn.execute("ALTER TABLE sale_entries ADD COLUMN external_id TEXT")
    conn.execute("ALTER TABLE sale_entries ADD COLUMN excluded INTEGER")
    conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS ux_sale_entries_external"
                 " ON sale_entries(external_id)")


MIGRATIONS: List[Migration] = [
    Migration(n=1, name="结构基线", apply=_m001),
    Migration(n=2, name="采集尝试表 fetch_attempt", apply=_m002),
    Migration(n=3, name="erp_sales 制单时间索引", apply=_m003,
              # ⚠ 表还没建（全新库 / 还没抓过云商）⇒ 这条**下次再来**，
              #   而不是"跳过就算完"（那会让索引永远建不出来）
              need=lambda db: (_table_exists(db, "erp_sales"),
                               "还没有 erp_sales 表（先跑一次抓取）")),
    Migration(n=4, name="利润结果表 profit_result", apply=_m004),
    Migration(n=5, name="收银流水 sale_entries", apply=_m005),
    Migration(n=6, name="政策快照 price_policy", apply=_m006),
    Migration(n=7, name="收银流水补列 SN/配件/支付/玲珑", apply=_m007),
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


#: 已跑编号的**记账键**（逗号分隔，如 `"1,2,4,5,6"`）。
#: `meta.schema` 仍记"已跑最大编号"（显示 + 老代码认它），但**判跑没跑只认这个集合**。
APPLIED_KEY = "migrations"


def applied(conn) -> set:
    """**已跑过哪些编号** —— "跳过没跳过"只认这个集合，不认 `schema` 的大小。

    ⚠ 只读：`meta` 表不在 / 连接是只读的（健康自检第二阶段就是 `mode=ro`）
      ⇒ 当空集合，再按 `schema` 补账（老库语义：1..schema 真都跑过）。
    """
    try:
        row = conn.execute("SELECT value FROM meta WHERE key=?",
                           (APPLIED_KEY,)).fetchone()
    except sqlite3.Error:
        row = None
    if row and str(row[0]).strip():
        out = set()
        for part in str(row[0]).split(","):
            try:
                out.add(int(part.strip()))
            except ValueError:
                continue
        if out:
            return out
    return set(range(1, current(conn) + 1))


def _mark(conn, done: set) -> None:
    """记账（一条一提交的前半步）：集合 + max 编号一起写。"""
    conn.execute("INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)",
                 (APPLIED_KEY, ",".join(str(n) for n in sorted(done))))
    if done:
        _set_current(conn, max(done))


# ------------------------------------------------------------------ 跑
def run(conn, *, migrations: Optional[List[Migration]] = None) -> dict:
    """把**没跑过**的迁移按编号顺序跑一遍。

    返回 `{"from", "to", "applied", "skipped"}`；失败抛 `MigrationError`。

    ⚠ **一条一提交**：中断时库里停在**上一条**的账上，下次从那儿接着跑。
    ⚠ 判"跑没跑"看 `applied()` 集合（**不是** `n > schema`）——
      后者会让"先跑后面的、前面还欠着"的那条永远补不上（见文件头）。
    """
    todo = sorted(migrations if migrations is not None else MIGRATIONS, key=lambda m: m.n)
    _ensure_meta(conn)
    start = current(conn)
    done = applied(conn)
    ran, skipped = [], []
    for m in todo:
        if m.n in done:
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
        done.add(m.n)
        _mark(conn, done)
        conn.commit()                                          # ⚠ 一条一提交
        ran.append({"n": m.n, "name": m.name})
    return {"from": start, "to": current(conn), "applied": ran, "skipped": skipped}


def status(conn) -> dict:
    """现在第几版、还欠几条 —— **只读**，`selftest` 和看板都调它。"""
    now = current(conn)
    done = applied(conn)
    out = {"schema": now, "pending": [], "skipped": []}
    for m in sorted(MIGRATIONS, key=lambda x: x.n):
        if m.n in done:
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
