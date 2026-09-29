"""四池的**存储层**：建表 / 写 / 读（M15 / 阶段 3.5）。

⚠ 纯规则的谓词与标签在 `rules.py`，导出在 `export.py`，文案在 `text.py` ——
这一层只管"表长什么样、怎么进怎么出"。
"""

from __future__ import annotations

import datetime
import sqlite3
from pathlib import Path

# ⚠ 四个点：本文件在 src/features/compliance/comparison/ 下（2026-09-19 往里挪了一层，
#   当时就是因为这个少了一个点而报 ModuleNotFoundError）
from ....dump import clear_col_cache, colname, ensure_columns, jd, put

from . import category as CAT
from .rules import (CLOSED_STATUS, DETAIL_COLS, QUADRANT_LABELS, RETURN_BILL_TYPES,
                       SALE, SAMPLE_MARK, combine, event_kind, is_sample_marker,
                       net_sold)

class PoolError(RuntimeError):
    """池子相关的问题。**别用 SystemExit** —— 本模块会被进程内调用（AGENTS.md 坑 11）。"""

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

#: `erp_sales` 上**必须有**的串号列 —— 销售报表 `SALES_COLUMNS` 已请求
#: `Imei/Imei2/Imei3`（→ 中文表头 串号/串号2/串号3）。老库是加列之前建的，
#: `ensure_columns` 只在**写入行里带这个键**时才 ALTER；空值行也要列在，
#: 否则待领 `SN_SQL_CANDIDATES` 扫不到、SQL 也不会 `no such column`。
#: ⚠ 实测销售报表副串号**值常为空**（2026-09-23）—— 列先建好，有值就能进。
SALES_SERIAL_COLS = ("串号", "串号2", "串号3")


def _ensure_sales_serial_cols(conn: sqlite3.Connection) -> None:
    """给 `erp_sales` 补齐三串号列（已存在则跳过）。"""
    try:
        have = {str(r[1]) for r in conn.execute(
            "PRAGMA table_info(erp_sales)")}
    except sqlite3.OperationalError:
        return
    if not have:
        return
    for col in SALES_SERIAL_COLS:
        if col not in have:
            # 列名含中文 —— 必须加引号（同 ensure_columns）
            conn.execute('ALTER TABLE erp_sales ADD COLUMN "%s" TEXT' % col)
            have.add(col)


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
    _ensure_sales_serial_cols(conn)
    conn.commit()

def today() -> str:
    return datetime.date.today().isoformat()

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
            # ⚠⚠ 无串号行（贴膜 / 礼品 / 配件）**也要落库**（2026-09-22）——
            #   原来这里 `continue` 整行丢掉，于是 `erp_sales` 里**一条贴膜都没有**，
            #   防护膜达成只能去扫 `.sales_*.xlsx`（慢，且两套口径）。
            #   主键是 `(sn, document_no)` ⇒ 合成 `nosn:<单号>:<序号>` 当 sn；
            #   ⚠ 取「真串号」的路径必须排掉 `nosn:` 前缀（见 `is_real_sn`）。
            nosn += 1
            row = dict(base)
            row["sn"] = "nosn:%s:%d" % (doc or "_", nosn)
            row["document_no"] = doc
            put(conn, table, row)
            written += 1
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

def replace_sales(conn: sqlite3.Connection, pool: str, rows: list[dict], *,
                  sn_field: str = "串号", doc_field: str = "单号") -> tuple:
    """**整表重写**（2026-09-29 用户：「设置里面加个强制刷新按钮吧，
    按照新规则全部重写数据库」）。

    为什么光 `save_sales`（`INSERT OR REPLACE`）不够：老口径写进去的行**删不掉** ——
    比如 2026-09-22 之前无串号的贴膜/礼包行压根没入库，之后的改法也只会"补新的"，
    旧规则留下的行会一直躺在表里。要"按新规则全部重写"就得**先清后写**。

    ⚠ **顺序是安全的关键**：
      1. 调用方**先抓全**（`rows` 已经在手上）才走到这儿 —— 抓失败时旧数据一行不动；
      2. **0 行直接拒绝**：抓了个空还去清库 = 把门店的数清没了；
      3. `DELETE` 和写入在**同一个事务**里（`save_sales` 末尾 commit），
         写到一半抛异常就 `rollback()` —— 不会留下"删了没写完"的空表。

    返回 `(写入行数, 拆出的串号数, 无串号行数)`（同 `save_sales`）。
    """
    table, is_snap = POOLS[pool]
    if is_snap:
        raise PoolError(f"{pool} 是快照池，整表重写请用 save_snapshot")
    if not rows:
        # 别拿一次空结果去清库 —— 那是"看着成功、数没了"最坏的一种失败
        raise PoolError(f"{pool} 这次抓到 0 行，拒绝清空旧表（旧数据一行没动）")
    conn.commit()                      # 收掉可能开着的隐式事务，别把 DELETE 混进去
    try:
        conn.execute('DELETE FROM "%s"' % table)
        res = save_sales(conn, pool, rows, sn_field=sn_field, doc_field=doc_field)
    except Exception:
        conn.rollback()                # 删了没写完 → 回滚，旧数据还在
        raise
    return res

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

def _pool_rows(conn: sqlite3.Connection, table: str, need, sql: str) -> list:
    """查品类列；**表或列不在 ⇒ 空**（= 这个池没有品类信息）。

    ⚠ 这里**故意不靠 `except OperationalError` 吞 `no such column`** ——
      那样会把"我把列名写错了"一起吞掉，表现是品类过滤整池失效、还没人知道。
      先 `PRAGMA` 问清楚列在不在，再查：**这种情况下 SQL 真出错就必须炸**
      （跟 `_latest_sn_set` 里"只吞表没建"是同一条规矩）。
    """
    if not set(need) <= _table_cols(conn, table):
        return []
    return list(conn.execute(sql))

def _category_maps(conn: sqlite3.Connection):
    """四个池各取 `{串号: 品类}`，顺手把**没见过的词**收上来。

    每个池的字段和取值方式都不一样（池A 只有内部编码、池D 只能看商品名前缀），
    **判断全在 `category.py`**（纯函数，只 import 标准库）—— 这儿只负责取。

    ⚠ 取法和 `_latest_sn_set` / `_sold_sns` **对齐**（快照取最新一天、云商在库取
      四列串号、剔掉 `-`）—— 不对齐的话，过滤会把池里真实存在的串号判成"没品类"。

    ⚠ 两个 rank 分工（2026-09-23，缺一不可）：
      **池内**多行按 `CAT._rank_within`（六类优先 —— 同串号的手机行+Care+行
      要取手机）；**池间**冲突在 `CAT.merge` 里按 `CAT._rank_between`
      （其它优先 —— 表带不许被云商粗类盖进来）。
    """
    unknown: dict = {}
    maps: dict = {}

    def judge_word(table, word_of):
        """一个"按词判"的池 → `row -> (品类, 要收的原词|None)`。"""
        def j(row):
            word = word_of(row)
            cat = CAT.classify(word, table)
            w = str(word or "").strip() if cat == CAT.UNKNOWN else None
            return cat, w
        return j

    def take(source, rows, sn_idx, judge):
        got: dict = {}
        for row in rows:
            cat, w = judge(row)
            if w:                          # ⚠ 没见过的词**单独收着**，不静默归「其它」
                unknown[w] = unknown.get(w, 0) + 1
            for i in sn_idx:
                sn = str(row[i] or "").strip()
                if sn and sn != "-":
                    # ⚠ 池内多行：具体的赢（见 `CAT._rank_within` 注释）
                    if CAT._rank_within(cat) > CAT._rank_within(got.get(sn)):
                        got[sn] = cat
        maps[source] = got

    take("lg_sales",
         _pool_rows(conn, "order_lines", ("sn", "category_id"),
                    "SELECT sn, category_id FROM order_lines"),
         (0,), judge_word(CAT.TABLES["lg_sales"], lambda r: r[1]))
    take("lg_stock",
         _pool_rows(conn, "lg_stock", ("sn", "category_name"),
                    "SELECT sn, category_name FROM lg_stock WHERE snapshot_date = "
                    "(SELECT MAX(snapshot_date) FROM lg_stock)"),
         (0,), judge_word(CAT.TABLES["lg_stock"], lambda r: r[1]))
    # ⚠ 池C 走 `classify_sales`（商品名前缀优先、一级分类兜底）——
    #   只看 `一级分类` 会把表带判成穿戴（用户 2026-09-23 报的 bug）。
    take("erp_sales",
         _pool_rows(conn, "erp_sales", ("sn", "一级分类", "商品名称"),
                    'SELECT sn, "一级分类", "商品名称" FROM erp_sales'
                    " WHERE sn NOT LIKE 'nosn:%'"),
         (0,), lambda r: CAT.classify_sales(r[1], r[2]))
    take("erp_stock",
         _pool_rows(conn, "erp_stock", ("sn", "imei", "sub_imei", "sub_imei1", "pro_name"),
                    "SELECT sn, imei, sub_imei, sub_imei1, pro_name FROM erp_stock "
                    "WHERE snapshot_date = (SELECT MAX(snapshot_date) FROM erp_stock)"),
         (0, 1, 2, 3), judge_word(CAT.TABLES["erp_stock"], lambda r: CAT.head_of(r[4])))
    return maps, unknown

def category_scope(conn: sqlite3.Connection):
    """→ `(参与对账的串号集合, 排除统计)`；`keep` 为 `None` = **本次不按品类过滤**。

    口径（六个大类、一台机器一个品类、未知词要报出来）见 `category.py` 模块头。

    ⚠⚠ **一个品类都没读到 ⇒ 返回 `None`（不过滤），不是空集。**
    库里没有品类列（老库 / 测试库 / 玲珑接口改版不再返回 `category_id`）时，
    四个池的映射全空 ⇒ 若照常过滤，**四个池会被整体清空**，界面上是"四象限全 0"，
    看着像"库里没数据" —— 本项目最忌讳的那种失败（`_latest_sn_set` 那个坑就是它）。
    → 宁可不收窄，也**不能把数据清零**；真的退化了，`stats["_skipped"]` 里带着原因，
      推送那边会写出来（**不许静默**）。

    ⚠ 注意这跟"库里确实一台六类机器都没有"不是一回事：那种情况 `resolved` 非空、
      只是 `keep_set` 空 —— 照常过滤（该空就空）。
    """
    maps, unknown = _category_maps(conn)
    resolved = CAT.merge(maps)
    if not resolved:
        return None, {"_skipped": "四个池一个品类都没读到（品类列缺失？）—— 本次未按品类过滤",
                      "_unknown_words": dict(unknown)}
    return CAT.keep_set(resolved), CAT.summary(resolved, unknown)

def quadrants(conn: sqlite3.Connection, *, detail: bool = False) -> dict:
    """**四象限 —— 双平台的全部对账逻辑就在这儿。**

    ```
                  池C 云商销售单              池D 云商在库
    池A 玲珑销售单   AC  ✅ 都卖了               AD  ❌ 玲珑报了、云商没报
    池B 玲珑在库     BC  ❌ 云商报了、玲珑没报     BD  ✅ 都没卖
    ```

    `AD`：玲珑报了量（卖了、出库了），云商那台**还挂在库里** → **云商没报**。
    `BC`：云商卖了（有销售单），玲珑那台**还挂在库里** → **玲珑没报**。

    两条实测出来的前提，不满足会**静默漏报**：

    ⚠ **池C 必须按时间序净额冲销退货**（`rules.net_sold`，2026-09-23 起）——
    只剔退货行不冲销原单，卖→退货回库就是假 BC（参考库实测 4/21 行中招）。
    ⚠ **池A 必须只取「已完成 且 未退货未退款」** —— 退货关闭的单会伪装成
    `AD`（实测误报过 1 台：`77TYD26606002629` 报了量又退货，订单已关闭）。

    ⚠ 快照池取**最新一天**（`snapshot_date` 最大的那批），不跨天混。

    ⚠⚠ **品类范围**（用户 2026-09-21）：只有手机 / 穿戴 / 音频 / 平板 / 电脑 / 智慧屏
    这六类参与 —— 四个集合**取完之后统一过滤**，不是各池各判。
    **只在一侧过滤会造出假 AD/BC**，理由和取舍见 `category.py` 模块头。
    """
    a = _reported_sns(conn)                  # 池A 玲珑销售单
    b = _latest_sn_set(conn, "lg_stock", ("sn",))            # 池B 只认 sn
    c, c_sample = _sold_sns(conn)            # 池C 云商销售单（样机单列）
    d = _latest_sn_set(conn, "erp_stock",                    # 池D 三列串号取并集
                       ("sn", "imei", "sub_imei", "sub_imei1"))

    # ⚠ 品类范围：**四个集合一起过滤**（对称）。样机那组也要过，
    #   否则它会带着配件混进 `BC_样机` 那个计数里。
    #   `keep is None` = 品类列读不到，本次不按品类过滤（见 `category_scope`）。
    keep, _stats = category_scope(conn)
    if keep is not None:
        a = a & keep
        b = b & keep
        c = c & keep
        c_sample = c_sample & keep
        d = d & keep

    # ⚠ 集合运算那半是**纯函数**（`rules.combine`）—— 口径能脱离库单测
    out = combine(a, b, c, c_sample, d)
    if not detail:
        return dict((k, len(v)) for k, v in out.items() if k != "all")
    return out

def is_real_sn(s) -> bool:
    """真串号？—— `nosn:` 前缀是 `save_sales` 给无串号行合成的假键，**不是机器**。

    四池对账只认真串号（2026-09-22：无串号行开始落库之后）。
    """
    t = str(s or "").strip()
    return bool(t) and not t.startswith("nosn:")


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
                if s and s != "-" and is_real_sn(s):
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
    """池C：云商卖出去的串号 —— **时间序净额冲销退货**，并按样机拆成两组。

    返回 `(非样机, 样机)`。

    ⚠ **拆开而不是直接滤掉**：本项目最忌讳"少给了东西还不吭声" ——
    排除掉多少台必须留痕，`quadrants` 会把样机那组单列成 `BC_样机`。

    ⚠ **退货要冲销原销售行，不能只剔退货行**（2026-09-23 用户报障修的）：
    老口径 `单据类型 NOT IN 退货` 只把退货行自己剔掉，**原销售行还留着** ——
    卖→退→货回库后 C 有原单、B/D 都有货 ⇒ `BC = B∩C` 命中假差异
    （参考库实测 3112 个串号原单没冲销，当前 BC 21 行里 4 行中招）。
    现在按 `rules.net_sold` 比「最后一次销售 vs 最后一次退货」，
    卖→退→再卖的（实测 6HR0226528000127）**保留** —— 那是真差异。

    ⚠ **样机也过冲销**（退了货的样机两边都不该出现），而且**只看销售行**的标识 ——
    退货行的 `串号标识` 是复制原单的，拿它判样机会让"卖样机→退货"留成样机差异。

    ⚠ 一台机器可能有多行销售（多次卖）。**只要有一行是样机就算样机** ——
    业务上样机卖出去玲珑就报不了量，别的行是什么都不改变这件事。
    """
    try:
        # ⚠ 时间列**按实际存在的列拼**：支付时间是主时钟（实测 146960 行全非空、
        #   精确到秒），制单时间只做兜底；精简库 / 测试库没有制单时间列 ——
        #   硬写 `IFNULL("制单时间")` 会 `no such column` 炸掉整个池C（真炸过）。
        have = _table_cols(conn, "erp_sales")
        tcols = [c for c in ("支付时间", "制单时间") if c in have]
        if not tcols:
            time_expr = "''"
        elif len(tcols) == 1:
            time_expr = "IFNULL(\"%s\", '')" % tcols[0]
        else:
            # ⚠ COALESCE 在 SQLite 至少要两个参数（单参直接 OperationalError）
            time_expr = "COALESCE(" + ", ".join(
                "IFNULL(\"%s\", '')" % c for c in tcols) + ")"
        rows = conn.execute(
            'SELECT sn, "单据类型", "串号标识", %s FROM erp_sales' % time_expr)
        per: dict = {}
        for sn, kind, mk, t in rows:
            if not sn or not is_real_sn(sn):
                continue
            slot = per.get(sn)
            if slot is None:
                slot = per[sn] = {"ev": [], "marks": []}
            kind = event_kind(kind)
            slot["ev"].append((kind, str(t or "")))
            if kind == SALE:
                slot["marks"].append(mk)
        sold = {sn for sn, v in per.items() if net_sold(v["ev"])}
        sample = {sn for sn in sold
                  if any(is_sample_marker(m) for m in per[sn]["marks"])}
        return sold - sample, sample
    except sqlite3.OperationalError as e:
        # ⚠ **只吞"表还没建"，别的错误必须炸出来。**
        #   原先这里一律 `return set()` —— 于是 SQL 语法错也被吞成空集，
        #   四象限显示"全都是 0"，看着像"库里没数据"，实际是查询写错了。
        #   （实测踩过：表名 `lg-stock` 里的横线被 SQLite 当成减法 → 语法错。）
        if "no such table" in str(e).lower():
            return set()
        raise

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
        # ⚠ **明细查询也要套有效单条件**（2026-09-23）：`sns` 虽然来自过滤过的 A，
        #   但同一个串号可能既有有效单又有「已关闭+退货」单 —— 不筛的话
        #   明细带出的是**退掉那一单**的信息（实测 2 个串号命中），门店照着
        #   退货单去追，白跑。`ORDER BY doc_create_time` 升序 ⇒ merge 后行覆盖
        #   前行，最终显示**最新一笔有效单**。
        marks = ",".join("?" for _ in CLOSED_STATUS)
        merge(conn.execute(
            "SELECT TRIM(l.sn), l.item_name, o.store_name, o.document_no,"
            " o.doc_create_time, l.included_tax_amount, o.sales_assistant_id"
            " FROM order_lines l JOIN orders o ON o.document_no = l.document_no"
            " WHERE TRIM(l.sn) IN (%s)"
            "   AND o.status_name NOT IN (%s)"
            "   AND IFNULL(o.return_status, 0) = 0"
            "   AND IFNULL(o.refund_status, 0) = 0"
            " ORDER BY IFNULL(o.doc_create_time, '')" % (ph, marks),
            tuple(args) + tuple(CLOSED_STATUS)),
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
        # ⚠ **明细不列退货行**（2026-09-23）：`sns` 已经过了净额冲销，但同一个
        #   串号的退货行还在表里 —— 不筛的话明细显示出来是「单据类型=零售退」，
        #   用户看到的就是"没筛掉退货"。`ORDER BY 支付时间` 升序 ⇒ merge 后行
        #   覆盖前行，最终显示**最新一笔销售**。
        marks = ",".join("?" for _ in RETURN_BILL_TYPES)
        # ⚠ ORDER BY 的列同样按实际存在拼 —— 精简库 / 只写过部分字段的库
        #   没有 `支付时间` 时别整张明细炸掉（没有就保持原顺序）。
        order = (' ORDER BY IFNULL("支付时间", \'\')'
                 if "支付时间" in _table_cols(conn, "erp_sales") else "")
        merge(conn.execute(
            'SELECT sn, "商品名称", "门店", "支付时间", "单据类型", document_no,'
            ' "串号标识", "金额", "业务员" FROM erp_sales'
            ' WHERE sn IN (%s)'
            '   AND IFNULL("单据类型", \'\') NOT IN (%s)%s' % (ph, marks, order),
            tuple(args) + tuple(RETURN_BILL_TYPES)),
            ("云商机型", "云商门店", "云商时间", "云商单据类型", "云商单号",
             "云商标识", "云商金额", "云商业务员"))

    return [out[s] for s in sorted(out)]
