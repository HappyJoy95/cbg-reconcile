"""四个数据池 —— **存储层**（取数在 `cbg.py` / `erp.py`，编排在 `cli.py`）。

2026-09-17 与用户定的口径：

| 池 | 是什么 | 表 | 写入语义 |
|---|---|---|---|
| **A** | 当前门店·玲珑销售单 | `orders` / `order_lines`（**已有**，`dump.py` 抓） | 按窗口重拉，只增不改 |
| **B** | 当前门店·玲珑在库 | `lg_stock` | **快照**，每天一份 |
| **C** | 全部门店·云商销售单 | `erp_sales` | 累积，一行一个串号 |
| **D** | 全部门店·云商在库 | `erp_stock` | **快照**，每天一份 |

## 为什么四个池子同库

用户 2026-09-17：「这四个池子放一个sqlite吧。cbg-2026.db不也是拉取销售单据吗。」
—— 对，`out/cbg-2026.db` 装的就是池 A。四池同库 ⇒ 对账时四张表直接 JOIN，
不用 `ATTACH`；而且"一年一个库"的约定原样成立。
⚠ 库名里的 `cbg-` 是历史包袱（现在装的不只是华为数据），
改名要碰已经跑通的订单库代码，**不值得**，所以在 `meta` 里写清楚。

## 为什么列存全

用户 2026-09-17：「列存全吧，避免别的调用。」
—— 沿用 `dump.py` 那套：`row_from()` 把接口返回的每个字段都提成列，
`ensure_columns()` 在接口加字段时**当场补列**，不用回来改代码。
接口多给什么，库里就多一列。

## 快照为什么留 30 天

用户 2026-09-17：「快照历史保留一个月吧。」
—— 一份最新快照 + 30 天归档。归档的用处：某次抓取失败时能回退对比，
以及能看出"这台机器在库里挂了多久"的变化。
"""

from __future__ import annotations

import datetime
import sqlite3
from pathlib import Path

from .dump import clear_col_cache, colname, ensure_columns, jd, put

#: 少数列名跟"驼峰直转"不一致，在这儿对齐
POOL_RENAME = {}


def pool_colname(field: str) -> str:
    """接口字段名 → 列名。**跟 `dump.colname` 的差别只有一条：全大写的保持全小写直转。**

    ⚠ 为什么不能在 `dump.colname` 上改：那个函数服务**已有的订单表**，
    改它等于改老表的列名 —— 老数据对不上，静默出事。
    而云商导出（`IMEI1` / `IMEI2` / `IMEI3` / `SNCode`）走驼峰转换会得到
    `i_m_e_i1` 这种东西：难看、容易写错、也没法按直觉查。

    规则：
      `IMEI1`  → `imei1`      （全大写 + 数字 → 整体小写）
      `商店`    → `商店`        （中文原样）
      `storeCode` → `store_code`（驼峰 → 下划线，跟 dump 一致）
    """
    s = str(field).strip()
    if s and s.upper() == s and any(c.isalpha() for c in s):
        return s.lower()
    return colname(s)


def pool_row_from(obj, *, skip=(), extras=None) -> dict:
    """把接口返回的整份对象转成一行（**列名走 `pool_colname`**）。

    跟 `dump.row_from` 一样：标量原样，嵌套对象/数组转 JSON 字符串。
    """
    row = {}
    for k, v in (obj or {}).items():
        if k in skip:
            continue
        row[POOL_RENAME.get(k, pool_colname(k))] = jd(v) if isinstance(v, (dict, list)) else v
    if extras:
        row.update(extras)
    return row

#: 快照保留天数
SNAP_KEEP_DAYS = 30

#: 三个新表。**只建键列**，其余字段靠 `ensure_columns` 按接口返回动态补。
#:
#: ⚠ **快照表故意不设主键** —— 云商在库导出里有 `IMEI1` 为空的行（配件/物料），
#: 拿 `(snapshot_date, sn)` 做主键会让多个空串号**互相覆盖**，
#: 结果就是"库里行数比接口自报的少，还不报错"。快照本来就是
#: "先删当天再全量写入"，不需要主键去重，改用普通表 + 索引。
#: 累积表（`erp_sales`）相反 —— 它要"重拉覆盖"，必须有主键。
SCHEMA = """
CREATE TABLE IF NOT EXISTS lg_stock (
    snapshot_date TEXT NOT NULL,
    sn            TEXT NOT NULL);

CREATE INDEX IF NOT EXISTS ix_lg_stock_date ON lg_stock(snapshot_date);
CREATE INDEX IF NOT EXISTS ix_lg_stock_sn   ON lg_stock(sn);

CREATE TABLE IF NOT EXISTS erp_stock (
    snapshot_date TEXT NOT NULL,
    sn            TEXT NOT NULL);

CREATE INDEX IF NOT EXISTS ix_erp_stock_date ON erp_stock(snapshot_date);
CREATE INDEX IF NOT EXISTS ix_erp_stock_sn   ON erp_stock(sn);

CREATE TABLE IF NOT EXISTS erp_sales (
    sn            TEXT NOT NULL,
    document_no   TEXT NOT NULL,
    PRIMARY KEY (sn, document_no));
"""

#: 池名 → (表名, 是不是快照)
POOLS = {
    "lg-stock": ("lg_stock", True),
    "erp-stock": ("erp_stock", True),
    "erp-sales": ("erp_sales", False),
}

#: 池的中文名（打印用）
POOL_LABELS = {
    "lg-stock": "池B 玲珑在库",
    "erp-stock": "池D 云商在库",
    "erp-sales": "池C 云商销售单",
}


class PoolError(RuntimeError):
    """池子相关的问题。**别用 SystemExit** —— 本模块会被进程内调用（AGENTS.md 坑 11）。"""


# --------------------------------------------------------------------- 建表
def ensure(conn: sqlite3.Connection) -> None:
    """建三张新表。

    ⚠ 顺手清一次 `dump._COLS_CACHE` —— 它的键是 `(id(conn), 表名)`，
    而 `id()` 在连接释放后会被复用。自己 `sqlite3.connect()` 的人
    （比如测试里开内存库）不走 `dump.connect()`，就不会清，
    于是"表里明明没这个列、缓存却说有" → `ALTER` 被跳过 →
    写入 `OperationalError: table erp_sales has no column named 单号`。
    **测试当场抓到的，不是理论问题。**
    """
    clear_col_cache()
    conn.executescript(SCHEMA)
    conn.commit()


def today() -> str:
    return datetime.date.today().isoformat()


# ----------------------------------------------------------------- 写快照
def save_snapshot(conn: sqlite3.Connection, pool: str, rows: list[dict], *,
                  date: str = "", sn_field: str = "sn") -> int:
    """写一天的快照。**同一天重跑 = 覆盖**（先删当天再写，不留半份）。

    `rows` 是接口原样的 dict 列表 —— 每个字段都会成为列。
    `sn_field` 指定哪个字段是串号（写进主键列 `sn`）。
    """
    table, is_snap = POOLS[pool]
    if not is_snap:
        raise PoolError(f"{pool} 不是快照池，别用 save_snapshot")
    d = date or today()

    conn.execute("DELETE FROM %s WHERE snapshot_date = ?" % table, (d,))
    written = 0
    for raw in rows:
        row = pool_row_from(raw)
        sn = str(row.get(sn_field) or row.get("sn") or "").strip()
        if not sn:
            # ⚠ 没有串号的行**不丢也不写主键列**：写一个占位符，保留原始字段。
            #   丢掉的话"库里有多少行"就对不上接口自报的行数了（少给了还不吭声）。
            sn = "-"
        row["sn"] = sn
        row["snapshot_date"] = d
        put(conn, table, row)
        written += 1
    conn.commit()
    return written


# ----------------------------------------------------------------- 写销售
def save_sales(conn: sqlite3.Connection, pool: str, rows: list[dict], *,
               sn_field: str = "串号", doc_field: str = "单号") -> tuple:
    """写销售明细。**一行一个串号** —— 接口的"串号"列是「主串 空格 副串」挤在一起，
    拆开后每个串号一行（其他字段复制），对账时才能直接 JOIN。

    返回 `(写入行数, 拆出的串号数, 没有串号的原始行数)`。
    """
    table, is_snap = POOLS[pool]
    if is_snap:
        raise PoolError(f"{pool} 是快照池，别用 save_sales")

    written, sns, nosn = 0, 0, 0
    for raw in rows:
        base = pool_row_from(raw)
        doc = str(base.get(doc_field) or base.get("document_no") or "").strip()
        parts = str(raw.get(sn_field) or "").replace("，", " ").split()
        parts = [p.strip() for p in parts if len(p.strip()) >= 8]
        if not parts:
            nosn += 1
            continue
        for p in parts:
            row = dict(base)
            row["sn"] = p
            row["document_no"] = doc
            put(conn, table, row)
            written += 1
            sns += 1
    conn.commit()
    return written, sns, nosn


# --------------------------------------------------------------- 快照轮转
def purge_snapshots(conn: sqlite3.Connection, *, keep_days: int = SNAP_KEEP_DAYS) -> dict:
    """删掉超过 `keep_days` 天的快照。返回 `{池名: 删了几行}`。"""
    cut = (datetime.date.today() - datetime.timedelta(days=keep_days)).isoformat()
    out = {}
    for pool, (table, is_snap) in POOLS.items():
        if not is_snap:
            continue
        n = conn.execute("DELETE FROM %s WHERE snapshot_date < ?" % table, (cut,)).rowcount
        out[pool] = max(n, 0)
    conn.commit()
    return out


# ------------------------------------------------------------------- 查询
def snapshots(conn: sqlite3.Connection, pool: str) -> list[tuple]:
    """这个池子有哪些日期的快照 + 各多少行。"""
    table, is_snap = POOLS[pool]
    if not is_snap:
        return []
    return list(conn.execute(
        "SELECT snapshot_date, COUNT(*) FROM %s GROUP BY snapshot_date "
        "ORDER BY snapshot_date DESC" % table))


def latest(conn: sqlite3.Connection, pool: str) -> str:
    """最新快照的日期（没有就返回空串）。"""
    rows = snapshots(conn, pool)
    return rows[0][0] if rows else ""


#: 云商 `串号标识` 里的**样机**标记。
#:
#: 实测（2026-09-17，`erp_sales` 全表 101732 行的 `串号标识`）：
#:   `样,新` 1816 · `s,样,新` 47 · `s,样，新,新` 8 · `样机,N,新` 6 · `N,样,新` 6 …
#: ⚠ 三个坑，缺一个就会漏：
#:   ① **位置不固定**（`样,新` 和 `N,样,新` 都有）⇒ 必须按逗号拆开**逐段**比
#:   ② **中文逗号**（`s,样，新,新`）⇒ 两种分隔符都要切
#:   ③ **有「样机」这种写法**，不只是「样」⇒ 用 `startswith`
#: ⚠ 另有一列 `lg_stock.tag_name`，那是**营销标签**（精品推荐/热销/预售…），
#:   **跟样机无关** —— 别找错地方。
SAMPLE_MARK = "样"


def is_sample_marker(marker) -> bool:
    """云商 `串号标识` 里有没有样机标记。

    **业务含义**（用户 2026-09-17 原话）：
    「云商卖了样机开单，但是玲珑开不了」——
    样机在玲珑那边**报不了量**，所以它会**一直挂在玲珑在库**，天天出现在 BC 里。
    **那是误报，要排除。**
    """
    if not marker:
        return False
    for part in str(marker).replace("，", ",").split(","):
        if part.strip().startswith(SAMPLE_MARK):
            return True
    return False


#: 池C 里**不算"卖了"**的单据类型 —— 退货后货回库，会同时出现在 C 和 D，
#: 那**是正常的**；不剔会造出一堆假异常（实测 `C∩D=10` 里 7 个就是这么来的）。
RETURN_BILL_TYPES = ("零售退", "分销退")

#: 池A 里**不算"报了量"**的订单状态
CLOSED_STATUS = ("已关闭",)


def quadrants(conn: sqlite3.Connection, *, detail: bool = False) -> dict:
    """**四象限 —— 四池的全部对账逻辑就在这儿。**

    ```
                  池C 云商销售单              池D 云商在库
    池A 玲珑销售单   AC  ✅ 都卖了               AD  ❌ 玲珑报了、云商没报
    池B 玲珑在库     BC  ❌ 云商报了、玲珑没报     BD  ✅ 都没卖
    ```

    `AD`：玲珑报了量（卖了、出库了），云商那台**还挂在库里** → **云商没报**。
    `BC`：云商卖了（有销售单），玲珑那台**还挂在库里** → **玲珑没报**。

    两条实测出来的前提，不满足会**静默漏报**：

    ⚠ **池C 必须剔退货单**（`RETURN_BILL_TYPES`）。
    ⚠ **池A 必须只取「已完成 且 未退货未退款」** —— 退货关闭的单会伪装成
    `AD`（实测误报过 1 台：`77TYD26606002629` 报了量又退货，订单已关闭）。

    ⚠ 快照池取**最新一天**（`snapshot_date` 最大的那批），不跨天混。
    """
    a = _reported_sns(conn)                  # 池A 玲珑销售单
    b = _latest_sn_set(conn, "lg_stock", ("sn",))            # 池B 只认 sn
    c, c_sample = _sold_sns(conn)            # 池C 云商销售单（样机单列）
    d = _latest_sn_set(conn, "erp_stock",                    # 池D 三列串号取并集
                       ("sn", "imei", "sub_imei", "sub_imei1"))

    out = {
        "A": a, "B": b, "C": c, "D": d,
        "AC": a & c, "AD": a & d, "BC": b & c, "BD": b & d,
        # ⚠ **样机单列一项**，不混进 BC 也不静默丢掉 ——
        #   "云商卖了样机、玲珑报不了量"是**已知的正常情况**，
        #   但排除掉多少台必须让人看得见（本项目最忌讳"少给了还不吭声"）。
        "BC_样机": b & c_sample,
    }
    c_all = c | c_sample                     # 算"只在 C"时样机也算 C
    every = a | b | c_all | d
    for name, s in (("A", a), ("B", b), ("C", c_all), ("D", d)):
        others = set()
        for n2, s2 in (("A", a), ("B", b), ("C", c_all), ("D", d)):
            if n2 != name:
                others |= s2
        out["only_" + name] = s - others
    out["all"] = every
    if not detail:
        return dict((k, len(v)) for k, v in out.items() if k != "all")
    return out


def _latest_sn_set(conn: sqlite3.Connection, table: str, cols=("sn",)) -> set:
    """快照池最新一天的全部串号。

    ⚠ **云商侧要多列取并集。** 库存导出有三列串号（`imei` / `sub_imei` /
    `sub_imei1`），同一台机器登记在哪一列**不一定** —— 只取 `imei`
    会把一部分"云商其实有"的判成"云商没有"，于是在 `BC` / `只在 B`
    里造出**假漏报**（实测：只取 `imei` 比三列并集少了一大截，D 从 34870 掉到 23635）。

    ⚠ **玲珑侧只认 `sn`**（用户 2026-09-17 明确）：
    玲珑一行给三个号（`sn` + 纯数字 `imei1`/`imei2`），而云商 `Imei` 列里
    混装 sn 和 imei —— **拿玲珑的 `sn` 去比就对了**，把 `imei1`/`imei2`
    也塞进来等于把"同一台机器的两个号"当成两台机器。
    """
    try:
        day = conn.execute('SELECT MAX(snapshot_date) FROM "%s"' % table).fetchone()[0]
        if not day:
            return set()
        have = {r[1] for r in conn.execute('PRAGMA table_info("%s")' % table)}
        use = [c for c in cols if c in have]
        out: set = set()
        for c in use:
            # ⚠ 表名和列名**都要加引号**：池名带横线（`lg-stock`），
            #   裸写会被 SQLite 当成减法 → `near "-": syntax error`。
            #   这个错误又被下面的 `except OperationalError` 吞成空集 ——
            #   表现是"四象限全是 0"，看着像没数据。
            for (v,) in conn.execute(
                    'SELECT "%s" FROM "%s" WHERE snapshot_date = ?' % (c, table), (day,)):
                s = str(v or "").strip()
                if s and s != "-":
                    out.add(s)
        return out
    except sqlite3.OperationalError as e:
        # ⚠ **只吞"表还没建"，别的错误必须炸出来。**
        #   原先这里一律 `return set()` —— 于是 SQL 语法错也被吞成空集，
        #   四象限显示"全都是 0"，看着像"库里没数据"，实际是查询写错了。
        #   （实测踩过：表名 `lg-stock` 里的横线被 SQLite 当成减法 → 语法错。）
        if "no such table" in str(e).lower():
            return set()
        raise


def _reported_sns(conn: sqlite3.Connection) -> set:
    """池A：玲珑报了量的串号 —— **只算有效单**（已完成、未退货未退款）。"""
    try:
        marks = ",".join("?" for _ in CLOSED_STATUS)
        rows = conn.execute(
            "SELECT DISTINCT TRIM(l.sn) FROM order_lines l "
            "JOIN orders o ON o.document_no = l.document_no "
            "WHERE TRIM(l.sn) != '' "
            "  AND o.status_name NOT IN (%s) "
            "  AND IFNULL(o.return_status, 0) = 0 "
            "  AND IFNULL(o.refund_status, 0) = 0" % marks, CLOSED_STATUS)
        return {r[0] for r in rows if r[0]}
    except sqlite3.OperationalError as e:
        # ⚠ **只吞"表还没建"，别的错误必须炸出来。**
        #   原先这里一律 `return set()` —— 于是 SQL 语法错也被吞成空集，
        #   四象限显示"全都是 0"，看着像"库里没数据"，实际是查询写错了。
        #   （实测踩过：表名 `lg-stock` 里的横线被 SQLite 当成减法 → 语法错。）
        if "no such table" in str(e).lower():
            return set()
        raise


def _sold_sns(conn: sqlite3.Connection) -> tuple:
    """池C：云商卖出去的串号 —— **剔掉退货单**，并按样机拆成两组。

    返回 `(非样机, 样机)`。

    ⚠ **拆开而不是直接滤掉**：本项目最忌讳"少给了东西还不吭声" ——
    排除掉多少台必须留痕，`quadrants` 会把样机那组单列成 `BC_样机`。

    ⚠ **样机也剔退货**：退了货的样机两边都不该出现。

    ⚠ 一台机器可能有多行销售（多次卖）。**只要有一行是样机就算样机** ——
    业务上样机卖出去玲珑就报不了量，别的行是什么都不改变这件事。
    """
    try:
        marks = ",".join("?" for _ in RETURN_BILL_TYPES)
        rows = conn.execute(
            'SELECT sn, "串号标识" FROM erp_sales '
            'WHERE IFNULL("单据类型", \'\') NOT IN (%s)' % marks, RETURN_BILL_TYPES)
        sample_of: dict = {}
        for sn, mk in rows:
            if not sn:
                continue
            sample_of[sn] = sample_of.get(sn, False) or is_sample_marker(mk)
        sample = {s for s, v in sample_of.items() if v}
        return set(sample_of) - sample, sample
    except sqlite3.OperationalError as e:
        # ⚠ **只吞"表还没建"，别的错误必须炸出来。**
        #   原先这里一律 `return set()` —— 于是 SQL 语法错也被吞成空集，
        #   四象限显示"全都是 0"，看着像"库里没数据"，实际是查询写错了。
        #   （实测踩过：表名 `lg-stock` 里的横线被 SQLite 当成减法 → 语法错。）
        if "no such table" in str(e).lower():
            return set()
        raise


#: 两个"有事"的象限，以及它们的说法（推送里直接用人话）
QUADRANT_LABELS = {
    "AD": "玲珑报了、云商没报",
    "BC": "云商报了、玲珑没报",
}

#: 明细表的列顺序（Excel / 推送共用一份，别处再排一次就会两边不一致）
DETAIL_COLS = (
    ("sn", "串号"),
    ("direction", "方向"),
    ("问题", "问题"),
    ("玲珑机型", "玲珑机型"),
    ("玲珑门店", "玲珑门店"),
    ("玲珑仓", "玲珑仓"),
    ("玲珑库龄", "玲珑库龄"),
    ("玲珑单号", "玲珑单号"),
    ("玲珑时间", "玲珑时间"),
    ("玲珑金额", "玲珑金额"),
    ("云商机型", "云商机型"),
    ("云商门店", "云商门店"),
    ("云商仓", "云商仓"),
    ("云商库龄", "云商库龄"),
    ("云商单号", "云商单号"),
    ("云商时间", "云商时间"),
    ("云商单据类型", "云商单据类型"),
    ("云商状态", "云商状态"),
)


def _table_cols(conn: sqlite3.Connection, table: str) -> set:
    """表里**实际**有哪些列（表不存在就返回空集）。

    ⚠ 拼 SQL 前先问这个 —— 列是 `ensure_columns` 按接口返回动态建的，
    不同导出版本给的字段不全一样，硬编码列名会 `no such column` 炸掉整条查询。
    """
    try:
        return {r[1] for r in conn.execute('PRAGMA table_info("%s")' % table)}
    except sqlite3.OperationalError:
        return set()


def details(conn: sqlite3.Connection, quadrant: str) -> list[dict]:
    """某个象限的明细 —— 每条一行，**够门店照着处理**。

    * `AD`（玲珑报了、云商没报）：池A 那一单 + 池D 云商还挂在哪个仓
    * `BC`（云商报了、玲珑没报）：池C 那一单 + 池B 玲珑还挂在哪个仓

    两个方向取的字段**不一样**（一边是"单据"、另一边是"在库快照"），
    所以结果字典的键会缺一些 —— 打印/导出时按 `DETAIL_COLS` 取，缺的留空。
    """
    q = (quadrant or "").upper()
    if q not in QUADRANT_LABELS:
        raise PoolError("只支持 AD / BC，给的是 %r" % quadrant)
    sns = quadrants(conn, detail=True)[q]
    if not sns:
        return []
    args = tuple(sorted(sns))
    ph = ",".join("?" for _ in args)
    out = {s: {"sn": s, "direction": q, "问题": QUADRANT_LABELS[q]} for s in sns}

    def merge(rows, fields):
        for r in rows:
            d = out.get(str(r[0]).strip())
            if d is None:
                continue
            for k, v in zip(fields, r[1:]):
                if v not in (None, ""):
                    d[k] = v

    if q == "AD":
        # ⚠ 金额取**行金额** `l.included_tax_amount`，不是整单的 `o.paid_amount` ——
        #   一单多行时（比如买手机送移动电源），整单金额会挂到赠品那行上，
        #   看着像"这个移动电源 5999 元"。
        merge(conn.execute(
            "SELECT TRIM(l.sn), l.item_name, o.store_name, o.document_no,"
            " o.doc_create_time, l.included_tax_amount, o.sales_assistant_id"
            " FROM order_lines l JOIN orders o ON o.document_no = l.document_no"
            " WHERE TRIM(l.sn) IN (%s)" % ph, args),
            ("玲珑机型", "玲珑门店", "玲珑单号", "玲珑时间", "玲珑金额", "玲珑店员"))
    else:
        merge(conn.execute(
            "SELECT sn, item_name, warehouse_name, stock_age FROM lg_stock"
            " WHERE snapshot_date = (SELECT MAX(snapshot_date) FROM lg_stock)"
            "   AND sn IN (%s)" % ph, args),
            ("玲珑机型", "玲珑仓", "玲珑库龄"))

    if q == "AD":
        # ⚠ 云商侧**按实际存在的列来比**，而且三列串号都要比 ——
        #   同一台机器登记在 imei/sub_imei/sub_imei1 哪一列不一定；
        #   而硬编码列名在"某个导出版本没有那一列"时会直接
        #   `no such column: sub_imei` 炸掉整张明细。
        cols = _table_cols(conn, "erp_stock")
        keys = [c for c in ("sn", "imei", "sub_imei", "sub_imei1") if c in cols]
        where = " OR ".join("%s IN (%s)" % (c, ph) for c in keys) or "0"
        merge(conn.execute(
            "SELECT sn, pro_name, store_name, ages, status FROM erp_stock"
            " WHERE snapshot_date = (SELECT MAX(snapshot_date) FROM erp_stock)"
            "   AND (%s)" % where, args * max(len(keys), 1)),
            ("云商机型", "云商仓", "云商库龄", "云商状态"))
    else:
        merge(conn.execute(
            'SELECT sn, "商品名称", "门店", "支付时间", "单据类型", document_no,'
            ' "串号标识", "金额", "业务员" FROM erp_sales WHERE sn IN (%s)' % ph, args),
            ("云商机型", "云商门店", "云商时间", "云商单据类型", "云商单号",
             "云商标识", "云商金额", "云商业务员"))

    return [out[s] for s in sorted(out)]


def export_xlsx(conn: sqlite3.Connection, path) -> tuple:
    """把 AD/BC 明细写成 Excel（两个 sheet）。返回 `(路径, {象限: 条数})`。

    ⚠ 表名有 **31 字符上限**（Excel 的硬限制），所以只取 `QUADRANT_LABELS` 的前半截。
    """
    import openpyxl

    wb = openpyxl.Workbook()
    wb.remove(wb.active)
    counts = {}
    for quad in ("AD", "BC"):
        rows = details(conn, quad)
        counts[quad] = len(rows)
        ws = wb.create_sheet("%s %s" % (quad, QUADRANT_LABELS[quad])[:31])
        ws.append([label for _, label in DETAIL_COLS])
        for r in rows:
            ws.append([r.get(k, "") if r.get(k) is not None else "" for k, _ in DETAIL_COLS])
        ws.freeze_panes = "A2"
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    wb.save(str(p))
    return p, counts


def notify_lines(conn: sqlite3.Connection, *, limit: int = 20, marks=None) -> list:
    """推送文案的正文行 —— **纯文本**，邮件和企微共用一份。

    两段，各带条数；每条一行，串号 + 机型 + 门店 + 单号/库龄。
    超过 `limit` 台就只列前几台并说明"详见 Excel"（推送不是报表，
    几十台堆在群里没人看；完整清单走附件）。

    `marks` = `pools_notify.annotate()` 的结果。**第一次出现的打 `★`**，
    推过的弱化并标出"已推 N 次、上次几号" —— 批发单那种
    「云商 9-14 报、玲珑要求 9-18 报」会连着推好几天，
    不区分的话门店第二天就不看了，**那几天新冒出来的反而被淹掉**。
    """
    out = []
    for quad in ("AD", "BC"):
        rows = details(conn, quad)
        if not rows:
            continue
        fresh = sum(1 for r in rows if ((marks or {}).get(r["sn"]) or {}).get("new", True))
        head = "【%s】%s —— %d 台" % (quad, QUADRANT_LABELS[quad], len(rows))
        if marks is not None:
            head += "（其中新出现 %d 台）" % fresh
        out.append(head)
        for r in rows[:limit]:
            m = (marks or {}).get(r["sn"]) or {}
            fresh = marks is None or m.get("new", True)
            model = str(r.get("玲珑机型") or r.get("云商机型") or "")[:40]
            store = (r.get("玲珑门店") or r.get("云商门店")
                     or r.get("玲珑仓") or r.get("云商仓") or "")
            doc = r.get("云商单号") or r.get("玲珑单号") or ""
            age = r.get("玲珑库龄") or r.get("云商库龄") or ""

            # ⚠ **`★` 顶到行首**，不是缀在行尾 —— 用户 2026-09-17 实测反馈
            #   「标注不是很醒目」。缀在后面等于没有，扫的时候看不见。
            #   企微那边会把 ★ 开头的整行染成橙红（`wecom.build_pools_markdown`）。
            out.append(("%s %s  %s" % ("★" if fresh else " ", r["sn"], model)).rstrip())
            tail = []
            if store:
                tail.append("门店 %s" % store)
            if age:
                tail.append("挂了 %s 天" % age)
            if marks is not None and not fresh and m.get("first"):
                # 「首次」不是「上次」—— 见 `pools_notify.annotate` 的注释
                tail.append("已推 %d 次，首次 %s" % (m.get("count") or 1, m["first"]))
            if tail:
                out.append("    " + " · ".join(tail))
            if doc:
                out.append("    单号 %s" % doc)
            out.append("")            # 条目之间空一行，不然糊成一坨
        if len(rows) > limit:
            out.append("  …另有 %d 台，见附件 Excel" % (len(rows) - limit))
        out.append("")
    return out


def status(conn: sqlite3.Connection) -> list[tuple]:
    """四个池子各多少行 / 最新到哪天。返回 `[(池名, 说明, 行数)]`。"""
    out = []
    for pool, (table, is_snap) in POOLS.items():
        try:
            n = conn.execute("SELECT COUNT(*) FROM %s" % table).fetchone()[0]
        except sqlite3.OperationalError:
            out.append((POOL_LABELS[pool], "（表还没建）", 0))
            continue
        if is_snap:
            days = snapshots(conn, pool)
            newest = days[0][0] if days else "—"
            out.append((POOL_LABELS[pool],
                        "快照 %d 天，最新 %s" % (len(days), newest), n))
        else:
            docs = conn.execute(
                "SELECT COUNT(DISTINCT document_no) FROM %s" % table).fetchone()[0]
            out.append((POOL_LABELS[pool], "%d 张单据" % docs, n))
    # 池 A 在 orders / order_lines
    try:
        o = conn.execute("SELECT COUNT(*) FROM orders").fetchone()[0]
        l = conn.execute("SELECT COUNT(*) FROM order_lines").fetchone()[0]
        out.append(("池A 玲珑销售单", "%d 张单据 / %d 明细行" % (o, l), o))
    except sqlite3.OperationalError:
        out.append(("池A 玲珑销售单", "（表还没建）", 0))
    return out
