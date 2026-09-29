"""分销看板的本地库 —— `out/distribution.db`（两张表）。

| 表 | 是什么 | 写入方 |
|---|---|---|
| `dist_sales` | 用户选定时间段现拉回来的**分销单明细**（单据类型 ∈ {分销, 分销退}，门店=渠道分销部） | `fetch.py` 区间覆盖写 |
| `region_map`  | **客户名 → 九区** 的人工确认映射（跨时段沿用、可改可重置） | 明细页下拉确认 |

## 为什么是独立库、不进 `cbg-<年>.db`

* `out/cbg-2026.db` 是**对账的地盘**（池A~D，`erp_sales` 在里面）——
  2.3.0 开发目标红线「不动现有功能」⇒ 一个表都不加。
* `out/` = 这台机器产出（AGENTS 的 in/out 分工）；selfupdate 不碰 `out/`。

## 区间覆盖写的口径（M1 的核心）

拉 `[start, end]` 之后：**先删掉 `支付时间` 落在该区间的行，再插入**。
重拉同一段 = 修正（补录、改单都收进来）；两段不相交的拉取互不影响。

⚠ 删的谓词必须和**服务端过滤的字段**一致：`sales_rows` 传
`StartDate/EndDate + TimeType=0`，实测返回行的 `支付时间` 全部落在区间内
（2026-09-29 用 1/25~1/31 回源核过）⇒ 按 `支付时间` 删是自洽的。
⚠ 实测 `支付时间` 无空值（渠道分销部 358/358）；真出现空值时它不会被任何
一次覆盖写删掉 —— 留在这个已知边界里，别假装覆盖是"绝对的"。
"""

from __future__ import annotations

import datetime
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from ...paths import ROOT
from ...storage import db as db_mod

#: 明细表列（中文表头 = 销售报表列名，和 `erp.SALES_COLUMNS` 对齐）。
#: ⚠ 只收看板/明细页用得到的列 —— 不整表搬运（47 列里成本系、发票系用不上，
#:   而且成本=考核价那套口径在这三张看板里根本不展示）。
NUMERIC = ("单价", "数量", "金额", "财务成本", "财务毛利", "优惠金额")

DDL_SALES = """CREATE TABLE IF NOT EXISTS dist_sales (
    支付时间 TEXT, 单号 TEXT, 单据类型 TEXT, 商品编码 TEXT, 商品名称 TEXT,
    一级分类 TEXT, 二级分类 TEXT, 三级分类 TEXT, 四级分类 TEXT, 品牌 TEXT,
    串号 TEXT, 单价 REAL, 数量 REAL, 金额 REAL, 财务成本 REAL, 财务毛利 REAL,
    付款方式 TEXT, 优惠金额 REAL, 门店 TEXT, 店员 TEXT, 业务员 TEXT,
    "客户/顾客" TEXT, "客户/顾客手机" TEXT, 供应商 TEXT, 备注 TEXT, 单行备注 TEXT
)"""

#: 索引：区间读 + 按客户找映射都走它。
DDL_SALES_IDX = ("CREATE INDEX IF NOT EXISTS ix_dist_sales_pay "
                 "ON dist_sales(支付时间)")

#: 客户 → 区。`zone` 空串 = 明确"清掉了"（区别于"从没确认过" —— 不过
#: 这里两种情况都等价于重新待确认，保留行只是为了留下 who/updated 痕迹）。
DDL_MAP = """CREATE TABLE IF NOT EXISTS region_map (
    customer TEXT PRIMARY KEY, zone TEXT NOT NULL DEFAULT '',
    who TEXT DEFAULT '', updated TEXT DEFAULT ''
)"""


def db_path(root: Optional[Path] = None) -> Path:
    return Path(root or ROOT) / "out" / "distribution.db"


def connect(root: Optional[Path] = None, *, write: bool = False):
    """开库。`write=False` 时**库不存在就返回 None**（= 还没拉过，不是错误）；
    `write=True` 才建目录、开可写连接。
    ⚠ 没走 `storage.db.read_only` —— 只读连接跑不了 `ensure` 的建表语句。"""
    p = db_path(root)
    if write:
        p.parent.mkdir(parents=True, exist_ok=True)
        conn = db_mod.connect(p)
    else:
        if not p.exists():
            return None
        conn = db_mod.connect(p, named=True)
    ensure(conn)
    return conn


def ensure(conn) -> None:
    """建表（幂等）。
    ⚠ 这里没用 `storage.db.read_only` —— 只读连接上跑 `CREATE IF NOT EXISTS`
    会炸；而"库不存在"已经在 `connect(write=False)` 里挡掉了（返回 None）。"""
    conn.execute(DDL_SALES)
    conn.execute(DDL_SALES_IDX)
    conn.execute(DDL_MAP)
    conn.commit()


def _num(v) -> Optional[float]:
    """xlsx 里数字常常是字符串；转不出的当 None（别把 `'—'` 当 0.0 混进求和）。"""
    if v is None or v == "":
        return None
    try:
        return float(str(v).replace(",", ""))
    except (TypeError, ValueError):
        return None


def _row(raw: dict) -> dict:
    """导出表头 → 本表列。缺的列补 None；数值列过一遍 `_num`。"""
    out = {}
    for col in ("支付时间", "单号", "单据类型", "商品编码", "商品名称",
                "一级分类", "二级分类", "三级分类", "四级分类", "品牌", "串号",
                "付款方式", "门店", "店员", "业务员", "客户/顾客",
                "客户/顾客手机", "供应商", "备注", "单行备注"):
        out[col] = (str(raw.get(col)).strip() if raw.get(col) is not None else None)
    for col in NUMERIC:
        out[col] = _num(raw.get(col))
    # 日期统一成字符串再比（xlsx 里可能是 datetime）
    t = raw.get("支付时间")
    if isinstance(t, (datetime.datetime, datetime.date)):
        out["支付时间"] = t.strftime("%Y-%m-%d %H:%M:%S")
    return out


def replace_range(root: Optional[Path], start: datetime.date, end: datetime.date,
                  rows: List[dict]) -> int:
    """区间覆盖写：删掉 `支付时间 ∈ [start, end]` 的旧行，插入这批新行。

    ⚠ 一个事务里做完（先删后插）—— 中途崩不能留下"删了没插"的空洞。
    返回写入行数（0 也是合法结果：这段真没有分销单）。
    """
    conn = connect(root, write=True)
    lo = start.isoformat()
    hi = (end + datetime.timedelta(days=1)).isoformat()      # 支付时间带时分秒 ⇒ 上界开区间
    keep = [_row(r) for r in rows]
    # 单号级去重交给"整段覆盖"；同一段内 API 本身不会返回重复行
    conn.execute('DELETE FROM dist_sales WHERE 支付时间 >= ? AND 支付时间 < ?', (lo, hi))
    cols = list(_row({}).keys())
    head = ", ".join('"%s"' % c for c in cols)
    ph = ", ".join("?" for _ in cols)
    conn.executemany(
        'INSERT INTO dist_sales (%s) VALUES (%s)' % (head, ph),
        [tuple(r.get(c) for c in cols) for r in keep])
    conn.commit()
    conn.close()
    return len(keep)


def read_rows(root: Optional[Path], start: datetime.date, end: datetime.date,
              store: str = "") -> List[Dict]:
    """按区间读明细（可再按门店筛）。库不存在/这段没拉过 ⇒ 空表（不是错误）。"""
    conn = connect(root, write=False)
    if conn is None:
        return []
    try:
        hi = (end + datetime.timedelta(days=1)).isoformat()
        sql = ('SELECT * FROM dist_sales WHERE 支付时间 >= ? AND 支付时间 < ?')
        args: list = [start.isoformat(), hi]
        if store:
            sql += ' AND 门店 = ?'
            args.append(store)
        sql += ' ORDER BY 支付时间'
        return [dict(r) for r in conn.execute(sql, args)]
    finally:
        conn.close()


def range_covered(root: Optional[Path], start: datetime.date,
                  end: datetime.date) -> Optional[Tuple[str, str]]:
    """这段拉过没有 —— 返回库里该区间实际的 `(最小支付时间, 最大支付时间)`。

    用来在界面上分清「拉过、是空」和「还没拉」（数据五态那条规矩：
    zero 和 missing 不能混，混了用户会把"没拉"当成"没卖货"）。
    没有行返回 None。
    """
    conn = connect(root, write=False)
    if conn is None:
        return None
    try:
        hi = (end + datetime.timedelta(days=1)).isoformat()
        row = conn.execute(
            'SELECT MIN(支付时间), MAX(支付时间) FROM dist_sales '
            'WHERE 支付时间 >= ? AND 支付时间 < ?',
            (start.isoformat(), hi)).fetchone()
        if row is None or row[0] is None:
            return None
        return (row[0], row[1])
    finally:
        conn.close()


# ---------------------------------------------------------------- 区域映射

def map_all(root: Optional[Path]) -> Dict[str, str]:
    """整张映射表 `{客户名: 区}`（zone 空串 = 清除过/没定，读的人当"待确认"）。"""
    conn = connect(root, write=False)
    if conn is None:
        return {}
    try:
        return {str(r[0]): str(r[1] or "")
                for r in conn.execute("SELECT customer, zone FROM region_map")}
    finally:
        conn.close()


def map_detail(root: Optional[Path]) -> List[Dict]:
    """映射表明细（谁、什么时候定的）—— 界面上"已确认列表 + 可改"那张表。"""
    conn = connect(root, write=False)
    if conn is None:
        return []
    try:
        return [{"customer": r[0], "zone": r[1] or "", "who": r[2] or "",
                 "updated": r[3] or ""}
                for r in conn.execute(
                    "SELECT customer, zone, who, updated FROM region_map "
                    "ORDER BY updated DESC")]
    finally:
        conn.close()


def map_set(root: Optional[Path], customer: str, zone: str, who: str = "") -> bool:
    """确认/修改/清除一个客户的区。`zone=""` = 清除（回到待确认）。

    返回是否真的变了（值相同 = 没变，别记一笔假操作）。
    """
    name = str(customer or "").strip()
    if not name:
        return False
    z = str(zone or "").strip()
    conn = connect(root, write=True)
    try:
        cur = conn.execute("SELECT zone FROM region_map WHERE customer = ?", (name,))
        got = cur.fetchone()
        if got is not None and str(got[0] or "") == z:
            return False
        conn.execute(
            "INSERT INTO region_map (customer, zone, who, updated) VALUES (?,?,?,?) "
            "ON CONFLICT(customer) DO UPDATE SET zone=excluded.zone, "
            "who=excluded.who, updated=excluded.updated",
            (name, z, str(who or ""),
             datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")))
        conn.commit()
        return True
    finally:
        conn.close()


def map_reset(root: Optional[Path]) -> int:
    """整张映射表清空（用户要的"可重置"）。返回删除行数。"""
    conn = connect(root, write=True)
    try:
        n = conn.execute("DELETE FROM region_map").rowcount
        conn.commit()
        return n
    finally:
        conn.close()
