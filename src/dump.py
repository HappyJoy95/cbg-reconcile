"""把 CBG 上**能抓到的数据**一次性落进 SQLite。

跟 `grab.py` 的区别：`grab.py` 只落「订单号 + SN + 金额 + 产品 + 备注」一张表（够对账用）；
这个脚本把**所有投影里所有字段**都收进来，并用**订单级**为基准建表（金额不会因多 SN 重复）。

落什么（每张表都能追溯到一次接口调用）：

    stores            门店档案          ← GET  /isrp/sms/store-info/store-detail
    orders            订单（订单级）    ← POST /isrp/srs/sale-order/paged-list
    order_lines       明细行（带 SN）   ← POST /isrp/soms/sale-order        （唯一的 SN 来源）
    payments          支付明细          ← 上面两个（详情的更全，remark 用列表的）
    order_labels      业务标记          ← orderLabelList / remark / stateLabel / 会员标签
    returns           退货单            ← POST /isrp/soms/sale-return/paged-list
    return_lines      退货明细（带 SN） ← POST /isrp/soms/sale-return     （含 relatedDocNo=原单）
    return_refunds    退款流水          ← 退货单的 refunds[]
    payment_medias    收款方式字典      ← POST /isrp/sps/medias/members/v3
    fetch_log         每次抓取的记录    ← 本地

⚠ 三条口径（实测出来的，别绕开）：
  1. **金额是订单级** —— 所以存在 `orders.included_tax_amount`，`SUM()` 直接可用；
     想按 SN 看金额时**先按订单号去重**（视图 `v_order_amount` 已经帮你做了）。
  2. **SN 只在详情接口**，列表投影一个都没有 → 每个订单必须打一次详情。
  3. **配件（非串号商品）没有 SN** —— 那些订单在 `order_lines` 里 `sn` 为空串，
     **不要用 INNER JOIN SN 去统计单数**，会漏。

用法：
    python dump.py --year 2026 --store-code SCN343260   # ⭐ 这一年 → out/cbg-2026.db
    python dump.py --days 30                            # 近 30 天 → 落到今年的库
    python dump.py --all                                # 全部历史（跨年时要求分年抓）
    python dump.py --db out/自己指定.db                  # 显式指定库文件

## 库文件**一年一个**（2026-09-16 用户定的）

`out/cbg-<年>.db`。年份按**北京时间**算（`ts_year`），**不看本机时区** ——
门店机器的时区可能是错的。

⚠ **为什么按年、不按月**：按月分库时**跨月退货会断** —— 9 月退 8 月的货，
原单在 8 月库里，`returns.related_doc_no` join 不到。按年分就没这个问题。
只剩**跨年**那一个边界（1 月退去年 12 月的单），`selfcheck` 会把它报出来。

⚠ `--all` 抓到的数据**跨年时直接报错**并要求分年抓 —— 不会静默写错库。

## 三条硬约束（都是踩出来的）

  1. **列表接口必须翻页 + 断言。** 全部走 `list_all_pages()`：按 `pageVO.totalPages`
     翻完，并断言"抓到的行数 == 接口自报的 `totalRows`"，**对不上就抛
     `FetchIncomplete`、整个抓取失败**。以前退货只读 `curPage=1`，而 30 天窗口
     恰好不到一页，所以一直没暴露；`--all` 一开（447 单 = 3 页）立刻暴露。
  2. **抓完自证**：`selfcheck()` 每次跑完自动验，**不等就退出码 2**（见该函数注释）。
  3. **所有字段都提成列**：`row_from()` + `ensure_columns()` —— **不写字段映射表**，
     接口以后多给一个字段，库里自动多一列。这是"为了以后扩展用"落到代码上的样子。
     ⚠ `raw` 列照旧存整份 JSON（兜底）。
"""
from __future__ import annotations

import argparse
import datetime
import json
import re
import sqlite3
import sys
import time
from pathlib import Path

# ⚠ 融合后**不再自己找仓库根** —— 它就是 src/ 里的一个模块，
#   根目录和 `src/cli.py` 用的是同一个定义（`parents[1]`），别搞两套。
ROOT = Path(__file__).resolve().parent.parent

import requests                                                        
from .cbg import DETAIL_PATH, CbgClient, CbgError                   
from .session import CBG_BASE, CbgAuthError, CbgSession             

CST = datetime.timezone(datetime.timedelta(hours=8))
LIST_PATH = "/isrp/srs/sale-order/paged-list"
RETURN_PATH = "/isrp/soms/sale-return/paged-list"
RETURN_DETAIL_PATH = "/isrp/soms/sale-return"      # ← 退货单详情，SN 在这一层
RESV_RETURN_PATH = "/isrp/soms/reservation-return/paged-list"
MEDIA_PATH = "/isrp/sps/medias/members/v3"

# --------------------------------------------------------------------- 建表
SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);

CREATE TABLE IF NOT EXISTS fetch_log (
    run_id       INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at   TEXT, finished_at TEXT, days INTEGER,
    window_start TEXT, window_end TEXT, store_code TEXT,
    orders INTEGER, order_lines INTEGER, payments INTEGER,
    returns INTEGER, refunds INTEGER, errors INTEGER, note TEXT);

CREATE TABLE IF NOT EXISTS stores (
    store_code TEXT PRIMARY KEY, store_name TEXT, abbreviation TEXT, address TEXT,
    business_mode_cn TEXT, image_level TEXT, home_platform TEXT, city_level TEXT,
    province TEXT, city TEXT, county TEXT, longitude TEXT, latitude TEXT,
    working_hours TEXT, receipt_name TEXT, tax_bureau_code TEXT,
    org_name TEXT, org_path TEXT, supplier_name TEXT, customer_name TEXT,
    raw TEXT);

CREATE TABLE IF NOT EXISTS orders (
    document_no TEXT PRIMARY KEY, order_no TEXT, store_code TEXT, store_name TEXT,
    pos_id TEXT, device_no TEXT, document_source INTEGER, scenario_type INTEGER,
    business_type INTEGER, status INTEGER, status_name TEXT, pay_status INTEGER,
    return_status INTEGER, refund_status INTEGER, delivery_status INTEGER,
    cashier_id TEXT, sales_assistant_id TEXT, consumer_guide_name TEXT,
    included_tax_amount REAL, excluded_tax_amount REAL, tax_amount REAL,
    discount_amount REAL, off_amount REAL, deposit_amount REAL,
    exempt_amount REAL, freight_amount REAL, paid_amount REAL,
    doc_create_time TEXT, doc_create_ts INTEGER, create_time TEXT, outbound_time TEXT,
    business_mode_code TEXT, business_mode_cn TEXT, order_business_type TEXT,
    remark TEXT, order_label_list TEXT, state_label TEXT, member_label TEXT,
    neu_label TEXT, customer_role TEXT, role_discount TEXT,
    hw_user_id TEXT, user_hash TEXT, captcha_phone_hash TEXT,
    line_count INTEGER, sn_count INTEGER, raw TEXT);

CREATE TABLE IF NOT EXISTS order_lines (
    document_no TEXT NOT NULL, line_no INTEGER NOT NULL,
    sn TEXT, ean TEXT, sku TEXT, spu TEXT, bpart TEXT, category_id TEXT,
    item_name TEXT, quantity REAL, unit TEXT, unit_price REAL,
    included_tax_amount REAL, excluded_tax_amount REAL, tax_amount REAL, tax_rate REAL,
    discount_amount REAL, off_amount REAL, discount_reason TEXT,
    purchase_price REAL, gross_profit REAL, coa_number TEXT, verifiable_amount REAL,
    inventory_status INTEGER, quantity_returned REAL, unique_code_status INTEGER,
    PRIMARY KEY (document_no, line_no));

CREATE TABLE IF NOT EXISTS payments (
    document_no TEXT NOT NULL, payment_no TEXT NOT NULL,
    media_no TEXT, media_member_no TEXT, media_name TEXT, media_desc TEXT,
    payment_channel TEXT, payment_amount REAL,
    nat_subsidy_discount_amount REAL, change_amount REAL, refund_amount_total REAL,
    pay_stage INTEGER, status INTEGER, payment_time TEXT, remark TEXT,
    source TEXT, raw TEXT,
    PRIMARY KEY (document_no, payment_no));

CREATE TABLE IF NOT EXISTS order_labels (
    document_no TEXT NOT NULL, source TEXT NOT NULL, label TEXT NOT NULL,
    PRIMARY KEY (document_no, source, label));

CREATE TABLE IF NOT EXISTS returns (
    kind TEXT NOT NULL, document_no TEXT NOT NULL, order_no TEXT, store_code TEXT,
    store_name TEXT, pos_id TEXT, device_no TEXT, document_source INTEGER,
    status INTEGER, pay_status INTEGER, enterprise_pay_status INTEGER,
    included_tax_amount REAL, excluded_tax_amount REAL, tax_amount REAL,
    payment_amount REAL, deposit_amount REAL,
    return_reason INTEGER, return_reason_name TEXT,
    cashier_id TEXT, sales_assistant_id TEXT, doc_create_time TEXT, doc_create_ts INTEGER,
    business_mode_code TEXT, business_mode_cn TEXT, member_id TEXT, raw TEXT,
    -- ↓ 详情接口 POST /isrp/soms/sale-return 才有的
    related_doc_no TEXT, in_bound_status INTEGER, sales_assistant_name TEXT,
    scenario_type INTEGER, order_business_type TEXT, hw_user_id TEXT, user_hash TEXT,
    point_value REAL, reclaim_point_amount REAL,
    PRIMARY KEY (kind, document_no));

-- 退货明细：**SN 在这一层**（列表投影只给商品名，没有 SN）
CREATE TABLE IF NOT EXISTS return_lines (
    kind TEXT NOT NULL, document_no TEXT NOT NULL, line_no INTEGER NOT NULL,
    sn TEXT, ean TEXT, sku TEXT, spu TEXT, bpart TEXT, category_id TEXT,
    item_name TEXT, specification TEXT, quantity REAL, unit TEXT, unit_price REAL,
    included_tax_amount REAL, excluded_tax_amount REAL, tax_amount REAL, tax_rate REAL,
    verifiable_amount REAL, non_refundable_amount REAL, return_discount_amount REAL,
    deposit_amount REAL, service_goods INTEGER, saleable INTEGER, inventory_status INTEGER,
    -- ⭐ 退的是哪张原单的哪一行
    related_doc_no TEXT, related_doc_line_no INTEGER,
    first_doc_no TEXT, first_doc_line_no INTEGER,
    PRIMARY KEY (kind, document_no, line_no));


CREATE TABLE IF NOT EXISTS return_refunds (
    kind TEXT NOT NULL, document_no TEXT NOT NULL, refund_no TEXT NOT NULL,
    payment_no TEXT, refund_amount REAL, refund_time TEXT,
    media_no TEXT, media_member_no TEXT, media_name TEXT, media_desc TEXT,
    status INTEGER, nat_subsidy_discount_amount REAL, raw TEXT,
    PRIMARY KEY (kind, document_no, refund_no));

CREATE TABLE IF NOT EXISTS payment_medias (
    media_no TEXT NOT NULL, media_member_no TEXT NOT NULL,
    media_name TEXT, media_desc TEXT, pay_channel_name TEXT, currency TEXT,
    is_active INTEGER, is_broker INTEGER, open_cash_drawer INTEGER,
    declaration_required INTEGER, invoice_allowed INTEGER, raw TEXT,
    PRIMARY KEY (media_no, media_member_no));

CREATE INDEX IF NOT EXISTS idx_lines_sn ON order_lines(sn);
CREATE INDEX IF NOT EXISTS idx_return_lines_sn ON return_lines(sn);
CREATE INDEX IF NOT EXISTS idx_orders_time ON orders(doc_create_time);
CREATE INDEX IF NOT EXISTS idx_orders_pos ON orders(pos_id, device_no);

-- ---------------------------------------------------------------- 报表视图
-- 订单级金额 + SN 汇总（金额不会重复计数）
CREATE VIEW IF NOT EXISTS v_order_amount AS
SELECT o.document_no, o.order_no, o.doc_create_time, o.store_code,
       o.pos_id, o.device_no, o.document_source, o.status_name,
       o.included_tax_amount AS amount, o.remark, o.order_label_list,
       o.line_count, o.sn_count,
       (SELECT GROUP_CONCAT(l.sn, ',') FROM order_lines l
         WHERE l.document_no = o.document_no AND l.sn <> '') AS sns
FROM orders o;

-- POS 使用率的基础口径：按 收银端 / 设备 / 来源 分组
CREATE VIEW IF NOT EXISTS v_pos_usage AS
SELECT pos_id, device_no, document_source,
       COUNT(*) AS orders, SUM(included_tax_amount) AS amount
FROM orders GROUP BY pos_id, device_no, document_source ORDER BY orders DESC;

-- 按天
CREATE VIEW IF NOT EXISTS v_daily_sales AS
SELECT substr(doc_create_time, 1, 10) AS day,
       COUNT(*) AS orders,
       SUM(included_tax_amount) AS amount,
       SUM(CASE WHEN pos_id = 'web-pos' THEN 1 ELSE 0 END) AS web_pos_orders,
       SUM(CASE WHEN pos_id = 'app-pos' THEN 1 ELSE 0 END) AS app_pos_orders
FROM orders GROUP BY day ORDER BY day;

-- 支付方式汇总（⚠ 必须按 media_no + media_member_no 分组）
CREATE VIEW IF NOT EXISTS v_payment_summary AS
SELECT media_no, media_member_no, media_name, media_desc,
       COUNT(*) AS payments, SUM(payment_amount) AS amount
FROM payments GROUP BY media_no, media_member_no, media_name, media_desc
ORDER BY amount DESC;

-- 业务标记汇总（标签 ∪ 备注）
CREATE VIEW IF NOT EXISTS v_label_summary AS
SELECT source, label, COUNT(DISTINCT document_no) AS orders
FROM order_labels GROUP BY source, label ORDER BY orders DESC;

-- 退货单 + 它退的是哪些 SN / 哪张原单
CREATE VIEW IF NOT EXISTS v_return_with_sn AS
SELECT r.kind, r.document_no, r.order_no, r.doc_create_time,
       r.included_tax_amount AS amount, r.payment_amount, r.return_reason_name,
       r.pos_id, r.device_no, r.sales_assistant_name, r.related_doc_no,
       l.line_no, l.sn, l.item_name, l.ean, l.sku,
       l.related_doc_no AS line_related_doc_no, l.first_doc_no
FROM returns r LEFT JOIN return_lines l
  ON l.kind = r.kind AND l.document_no = r.document_no;
"""

# ⚠ 已经建过的老库要补列（CREATE TABLE IF NOT EXISTS 不会改结构）
MIGRATIONS = {
    "returns": [
        ("related_doc_no", "TEXT"), ("in_bound_status", "INTEGER"),
        ("sales_assistant_name", "TEXT"), ("scenario_type", "INTEGER"),
        ("order_business_type", "TEXT"), ("hw_user_id", "TEXT"),
        ("user_hash", "TEXT"), ("point_value", "REAL"), ("reclaim_point_amount", "REAL"),
    ],
}


def _ensure_columns(conn) -> None:
    for table, cols in MIGRATIONS.items():
        have = {r[1] for r in conn.execute("PRAGMA table_info(%s)" % table)}
        for name, typ in cols:
            if name not in have:
                conn.execute("ALTER TABLE %s ADD COLUMN %s %s" % (table, name, typ))
                print("  迁移：%s 补列 %s" % (table, name), flush=True)



# --------------------------------------------------------------------- 工具
def ts2str(ts) -> str:
    if not ts:
        return ""
    try:
        return datetime.datetime.fromtimestamp(int(ts), CST).strftime("%Y-%m-%d %H:%M:%S")
    except (ValueError, OSError, OverflowError):
        return ""


def jd(obj) -> str:
    return json.dumps(obj, ensure_ascii=False)


def range_ts(days: int) -> tuple:
    today = datetime.datetime.now(CST).date()
    a = datetime.datetime.combine(today - datetime.timedelta(days=days),
                                  datetime.time(0, 0, 0), tzinfo=CST)
    b = datetime.datetime.combine(today, datetime.time(23, 59, 59), tzinfo=CST)
    return int(a.timestamp()), int(b.timestamp())


def year_range(year: int) -> tuple:
    """某一年的起止（含最后一天 23:59:59）。"""
    a = datetime.datetime(year, 1, 1, 0, 0, 0, tzinfo=CST)
    b = datetime.datetime(year, 12, 31, 23, 59, 59, tzinfo=CST)
    return int(a.timestamp()), int(b.timestamp())


def month_range(ym: str) -> tuple:
    """某月的起止。**到"今天"为止，不是月末** —— 未来的日期没有数据可拉。

    ⚠ 这是**日常流程用的窗口**，用户原话：
    「拉当月是为了**避免 21:00 拉当天、后面还有更新**，所以**拉整月然后取并集**」。
    每天把当月重拉一遍，`INSERT OR REPLACE` 只增不改不删 ⇒ 21:00 之后的更新也收进来了。
    """
    y, m = int(ym[:4]), int(ym[5:7])
    a = datetime.datetime(y, m, 1, 0, 0, 0, tzinfo=CST)
    now = datetime.datetime.now(CST)
    if (now.year, now.month) == (y, m):
        b = now                                   # 当月 → 拉到此刻
    else:
        nxt = datetime.datetime(y + (m == 12), (m % 12) + 1, 1, tzinfo=CST)
        b = nxt - datetime.timedelta(seconds=1)   # 过去的月 → 拉到月末
    return int(a.timestamp()), int(b.timestamp())


def this_month() -> str:
    return datetime.datetime.now(CST).strftime("%Y-%m")


def ts_year(ts) -> int:
    """时间戳 → 年份（**按北京时间**，不是本机时区 —— 门店机器可能设错时区）。"""
    return datetime.datetime.fromtimestamp(int(ts), CST).year


def year_db(out_dir, year: int) -> Path:
    """一年一个库：`out/cbg-2026.db`。

    ⚠ **为什么按年不按月**（2026-09-16 用户先定按月、随后改成按年）：
    按月分库时**跨月退货会断** —— 9 月退 8 月的货，原单在 8 月库里 join 不到。
    按年分就没有这个问题（同年的原单和退货在同一个库）。
    只剩**跨年**那一个边界：1 月退去年 12 月的单 → `selfcheck` 会报出来。
    """
    return Path(out_dir) / ("cbg-%d.db" % year)


def put(conn, table: str, row: dict) -> None:
    """写一行。**表里没有的列先自动补上**（见 `ensure_columns`）。"""
    ensure_columns(conn, table, row)
    # ⚠ 列名加引号 —— 同 `ensure_columns`：云商表头里有 `69码` 这类非法标识符。
    cols = ", ".join('"%s"' % c for c in row)
    marks = ", ".join("?" for _ in row)
    conn.execute("INSERT OR REPLACE INTO %s (%s) VALUES (%s)" % (table, cols, marks),
                 list(row.values()))


# --------------------------------------------------- 通用提取（"全提成列"）
_CAMEL = re.compile(r"(?<!^)(?=[A-Z])")


def colname(field: str) -> str:
    """接口字段名 → 列名：驼峰转下划线、全小写。

    ⚠ **故意不写字段映射表。** 手写映射一定会漏，而且**接口加一个字段就得回来改**。
    2026-09-16 用户定"全部保留，为了以后扩展用" —— 这条就是那个决定落到代码上的样子：
    **接口多给什么，库里就多一列。**
    """
    return _CAMEL.sub("_", field).lower()


#: 这些字段**不进主体表** —— 它们各自建表，或者外面已经单独处理过
NESTED_SKIP = ("details", "payments", "refundLines", "invoiceVos")

#: 少数列名和"驼峰直转"不一致（历史列名 / 接口自己的拼写错），在这儿对齐
#:   `bussinessType` —— 华为那边多打了一个 s，库里一直叫 `business_type`
RENAME = {"bussinessType": "business_type"}


def row_from(obj, *, skip=NESTED_SKIP, extras=None) -> dict:
    """把接口返回的**整份对象**转成一行 —— 每个字段都提成列。

    * 标量 → 原样
    * 嵌套对象 / 数组 → **JSON 字符串**（照样占一列，以后要拆再拆）
    * `skip` 里的字段不进（它们单独建表）
    * `extras` 覆盖/补充算出来的列（如 `doc_create_time` / `line_count` / `raw`）
    """
    row = {}
    for k, v in (obj or {}).items():
        if k in skip:
            continue
        row[RENAME.get(k, colname(k))] = jd(v) if isinstance(v, (dict, list)) else v
    if extras:
        row.update(extras)
    return row


_COLS_CACHE = {}


def clear_col_cache() -> None:
    """清掉"这条连接上这张表有哪些列"的缓存。

    ⚠ **新开一个连接就必须清。** 缓存键是 `(id(conn), 表名)`，而
    `sqlite3.Connection` 既不支持弱引用、也不能挂属性（实测过），
    只能拿 `id()` 当键 —— 于是连接释放后 `id()` 被复用，
    "缓存里说这张表有这个列，新库其实没有" → `ALTER` 被跳过 →
    写入直接 `OperationalError: table X has no column named Y`。

    `connect()` 会自己调；**自己 `sqlite3.connect()` 的人（测试、`pools.ensure`）
    也必须调一次**，否则会串到上一个连接的列集合上。
    """
    _COLS_CACHE.clear()


def ensure_columns(conn, table: str, row: dict) -> None:
    """表里没有的列，**当场补上**。

    ⚠ 这是"以后扩展用"的关键：接口加一个字段，**不用回来改代码** ——
    下次抓取自己就把列建出来了（`ALTER TABLE ADD COLUMN` 不影响老数据）。

    ⚠ 缓存键是 **`(id(conn), 表名)` 而不是光表名** —— 光表名在
    "一个进程里开第二个库"时会骗人（见 `connect`）。`id()` 会在对象释放后
    被复用，所以 `connect()` 里必须**清空**缓存，两条一起才严密。
    """
    key = (id(conn), table)
    have = _COLS_CACHE.get(key)
    if have is None:
        have = {r[1] for r in conn.execute("PRAGMA table_info(%s)" % table)}
        _COLS_CACHE[key] = have
    for name, val in row.items():
        if name in have:
            continue
        typ = ("INTEGER" if isinstance(val, int) and not isinstance(val, bool)
               else "REAL" if isinstance(val, float) else "TEXT")
        # ⚠ 列名**必须加引号**：云商导出的表头里有 `69码` 这种数字开头的，
        #   裸写就是 `unrecognized token`。加引号对已有的英文列名零影响。
        conn.execute('ALTER TABLE %s ADD COLUMN "%s" %s' % (table, name, typ))
        have.add(name)


# ------------------------------------------------------- 分页 + 完整性断言
class FetchIncomplete(RuntimeError):
    """抓到的行数和接口自报的 `totalRows` 对不上。

    ⚠ **必须抛，不许打印了事。** 这正是"退货少了 3 张而工具报成功"的成因：
    没有任何东西比对过"接口说有几条"和"实际拿到几条"。
    """


def time_filter(body: dict, start, end) -> dict:
    """把时间窗放进 body；`start/end` 是 `None` 就**一个都不放** = 取全部历史。

    ⚠ 实测（2026-09-16，门店 SCN343260）：**不传时间窗 = 该店全部历史**，
    不是截断 —— 447 单 / 83 天（最早 2026-06-25），退货 7 张（最早 2026-07-11）。
    当初 `returns_probe` 里那个 `totalRows=7` 悬案就是这么来的：
    "最小 body" 必然把时间窗一起去掉了，所以看到的是**全历史**而不是 30 天。
    """
    if start is not None and end is not None:
        body["startTime"] = start
        body["endTime"] = end
    return body


def list_all_pages(request, body: dict, *, page_size: int = 200,
                   envelope: str = "result", label: str = "") -> list:
    """把分页接口**翻完**，并断言"抓到的行数 == 接口自报的 `totalRows`"。

    `request` = `callable(payload: dict) -> dict`，自己负责鉴权和信封差异
    （`medias` 那个接口没有 status/result，走不了 `CbgClient._request`）。

    ⚠ 三条实测教训：
      1. **不翻页 = 静默丢数据。** 2026-09-16 之前退货只读 `curPage=1`，
         而 30 天窗口恰好不到一页，所以一直没暴露；`--all` 一开（447 单）立刻暴露。
      2. **不比对 `totalRows` 就永远不会知道。** 0 条也要比。
      3. **宁可整个抓取失败，也不要交出一份"看起来成功、其实缺行"的库** ——
         下游拿它算合规，缺行是算错的，而且没人看得出来。
    """
    rows, page = [], 1
    declared = None
    while True:
        payload = dict(body)
        payload["curPage"] = page
        payload["pageSize"] = page_size
        j = request(payload) or {}
        batch = j.get(envelope) or []
        rows.extend(batch)
        pv = j.get("pageVO") or {}
        if pv.get("totalRows") is not None:
            declared = pv["totalRows"]
        total_pages = pv.get("totalPages") or 1
        if page >= total_pages or not batch:
            break
        page += 1
    if declared is None:
        print("    ⚠ %s：接口没给 totalRows，**无法断言抓全了**（拿到 %d 行）"
              % (label or envelope, len(rows)), flush=True)
    elif int(declared) != len(rows):
        raise FetchIncomplete(
            "%s：抓到 %d 行，接口自报 totalRows=%s —— 对不上，库不完整，中止。"
            % (label or envelope, len(rows), declared))
    return rows


# ------------------------------------------------- 给报量对账用的读接口
class DbStale(RuntimeError):
    """库不够新 —— 覆盖不了这次要查的窗口。

    ⚠ **必须抛，不许"将就用旧库"**：拿一份缺数据的库去算差集，
    会把当天所有销售都算成「未报量」—— 一份完全错误的清单，
    而且**看着很合理**，门店会照着去补报一批假的。
    """


def connect(path, *, named: bool = False) -> sqlite3.Connection:
    """**唯一**建连接的地方 —— 顺手把列缓存清掉。

    ⚠ **`named` 默认是 `False`（行是元组），这个默认值很要紧。**

    Python 的 `%` 格式化**只对元组展开**：`"%-9s %4d" % row` 在 `row` 是元组时
    正常，是别的东西时只允许**一个**占位符，多一个就
    `TypeError: not enough arguments for format string`。
    而 `dump.main` 末尾那段汇总打印（按 POS/设备、按支付方式…）全是这种写法。

    实测踩过：给主连接设了 `Row` 之后，门店跑「整个项目」时
    **数据其实已经写进库了**，却崩在最后的汇总打印上，退出码 9 →
    整条日常流程中止 → 报量排查和 POS 都没跑。**最坏的一种失败：
    活儿干完了，但工具说自己失败了。**

    `named=True` 给**读**的人用（`check_freshness` / `reported_sns_from_db`
    要 `r["sn"]` 这种按列名取）。

    ## 为什么非得集中到一处

    `_COLS_CACHE` 只能按 `id(conn)` 做键
    （`sqlite3.Connection` **既不支持弱引用、也不能挂属性**，实测过），
    而 `id()` 在连接释放后会被复用 —— 于是"缓存里说这张表有这个列，
    新库其实没有" → `ALTER` 被跳过 → 写入直接
    `OperationalError: table order_lines has no column named service_goods`。

    实测复现过（同一个进程开两个内存库，第二个必炸）。这不是理论问题：
    Web 是长驻进程，跨年那天它要建 `cbg-2027.db`，而缓存里还留着
    2026 那个库的列 —— 一年只错一次，最难查的那种。
    """
    clear_col_cache()
    conn = sqlite3.connect(str(path))
    if named:
        conn.row_factory = sqlite3.Row
    return conn


def open_db(path) -> sqlite3.Connection:
    """给**读**的人用：行按列名取（`r["sn"]`）。

    ⚠ 和 `connect()` 的默认值**故意不同**：这里的调用方全是按列名取的，
    而 `dump.main` 那边是按位置 `%` 展开的。一个开关两种用法，
    所以把这个差别摆在函数名上，别让它靠"记得传参数"。
    """
    return connect(path, named=True)


def check_freshness(conn, need_by_ts: int) -> tuple:
    """库够不够新？判据 = **最后一次抓到数据的时刻，不早于 need_by_ts**。

    ⚠ 只用 `finished_at` 不够 —— 抓到 0 张的那次也算"跑过"。
    所以要求那次 `orders > 0`（否则整库是空的，覆盖不了任何窗口）。

    返回 `(够不够, 为什么)` —— **第二个值是真正的病因**，别只返回 bool
    （这个项目在权限那个坑上吃过亏：只回一句"自检没过"，谁也定位不了）。
    """
    row = conn.execute(
        "SELECT finished_at, orders, order_lines, note FROM fetch_log"
        " WHERE orders > 0 ORDER BY run_id DESC LIMIT 1").fetchone()
    if row is None:
        return False, "库里没有任何成功的抓取记录"
    try:
        done = datetime.datetime.strptime(row["finished_at"], "%Y-%m-%d %H:%M:%S").replace(tzinfo=CST)
    except (TypeError, ValueError):
        return False, "抓取记录的时间读不出来：%r" % (row["finished_at"],)
    if int(done.timestamp()) < int(need_by_ts):
        return False, ("最后一次抓到数据是 %s，而这次要查的窗口到 %s —— "
                       "**中间的新单没抓进来**，先跑一次 dump"
                       % (row["finished_at"], ts2str(need_by_ts)))
    return True, "最后一次抓取 %s（%s 单）" % (row["finished_at"], row["orders"])


def reported_sns_from_db(conn, start_ts: int, end_ts: int) -> dict:
    """从库里取「已报量」SN 集合。

    **形状和 `cbg.CbgClient.reported_sns()` 逐字段一致** —— 对账那边才能无缝替换：
        {sn: {documentNo, orderNo, item, amount, time, guide}}

    ⚠ 两点和接口版**故意不同**（都是改进，不是偏差）：

    1. **不传 `returnStatus`** ⇒ 已退货/已关闭的原单**也在里面**。
       接口版传 `returnStatus=0` 会把它们整张滤掉，于是云商侧还在的销售
       会被误报成「未报量」。**库里存的是全部，从库读顺带修掉了这个误报。**
    2. **一个 SN 挂多张单时，取「非服务产品」那张；并列时取最早**。
       接口版是"后写覆盖先写"，取决于遍历顺序，**不确定** ——
       实测 4 个 SN 上，接口版取到的是 **HUAWEI Care+ 服务单**（499/899 元），
       而那张单不是"这笔销售"（真正的销售是机器单，6999 元）。
       报表里"已报量"那一栏会因此显示错误的产品名和金额。

       ⚠ `serviceGoods` 是**可靠标记**（实测：=1 的 6 行全部含 "Care+"，
       =0 的 498 行全部不含）—— 别用"商品名含 Care+"这种字符串判断。
    """
    # ⚠ 老库（2026-09-16 之前 dump 出来的那份）**没有 `service_goods` 列** ——
    #   那时候还没有"全提成列"。所以这里探测一下，缺了就退回商品名匹配。
    #   （新 dump 出来的库有这一列，走的是可靠路径。）
    cols = {x[1] for x in conn.execute("PRAGMA table_info(order_lines)")}
    if "service_goods" in cols:
        sg = "COALESCE(ol.service_goods, 0)"
    else:
        sg = "CASE WHEN ol.item_name LIKE '%Care+%' THEN 1 ELSE 0 END"
    out = {}
    for r in conn.execute(
            "SELECT ol.sn AS sn, ol.item_name AS item,"
            "       o.document_no AS document_no, o.order_no AS order_no,"
            "       o.included_tax_amount AS amount, o.doc_create_time AS time,"
            "       o.consumer_guide_name AS guide"
            "  FROM order_lines ol JOIN orders o ON o.document_no = ol.document_no"
            " WHERE ol.sn <> '' AND o.doc_create_ts BETWEEN ? AND ?"
            " ORDER BY (%s = 1) ASC, o.doc_create_ts ASC" % sg,
            (int(start_ts), int(end_ts))):
        if r["sn"] in out:                     # 先到的赢（= 非服务产品优先、再按时间最早）
            continue
        out[r["sn"]] = {
            "documentNo": r["document_no"], "orderNo": r["order_no"],
            "item": r["item"] or "", "amount": r["amount"],
            "time": r["time"] or "", "guide": r["guide"] or ""}
    return out


# --------------------------------------------------------------------- 各段
def selfcheck(conn) -> dict:
    """抓完自证 —— **每次跑完自动验，不等就非 0 退出**（不能只打印）。

    只放**硬指标**（放软指标会天天误报，然后就没有人看了）：

      1. ⭐ **订单金额合计 == 支付金额合计** —— 两边是各自独立取的，
         能对上就说明订单和支付**都没有缺行**。v1/v2 实测都差 **0.00**
         （634,718.67 / 656,504.67）。这是最硬的一条。
      2. **订单和支付都非空** —— 防"跑了但什么都没抓到"。

    另有一条**只报告、不判失败**的：退货单的原单有多少不在库里
    （窗口抓时这是**正常的**，全量抓时它应该等于 0）。

    ⚠ 列表接口"翻没翻完"不在这里 —— 那个由 `list_all_pages` 当场断言、
    当场抛 `FetchIncomplete`，比事后再查更早、更准。
    """
    out = {"lines": [], "ok": True}
    o_n, o_amt = conn.execute(
        "SELECT COUNT(*), ROUND(COALESCE(SUM(included_tax_amount),0),2) FROM orders").fetchone()
    p_n, p_amt = conn.execute(
        "SELECT COUNT(*), ROUND(COALESCE(SUM(payment_amount),0),2) FROM payments").fetchone()
    diff = round((o_amt or 0) - (p_amt or 0), 2)
    out["lines"].append("订单 %d 张 / %s   支付 %d 笔 / %s   两边差 %s"
                        % (o_n, o_amt, p_n, p_amt, diff))
    if abs(diff) > 0.01:
        out["ok"] = False
        out["lines"].append("❌ 订单金额 != 支付金额（差 %s）—— 有一边缺行" % diff)
    if not o_n or not p_n:
        out["ok"] = False
        out["lines"].append("❌ 订单或支付是空的 —— 这次抓取没拿到东西")

    r_n, orphan = conn.execute(
        "SELECT (SELECT COUNT(*) FROM returns),"
        " (SELECT COUNT(*) FROM returns r LEFT JOIN orders o"
        "    ON o.document_no = r.related_doc_no WHERE o.document_no IS NULL)").fetchone()
    out["lines"].append("退货 %d 张，其中**原单不在库里**的 %d 张%s"
                        % (r_n, orphan,
                           "（窗口抓时正常；`--all` 时应该是 0）" if orphan else ""))
    return out


def load_store(conn, client: CbgClient, store_code: str) -> dict:
    d = client.store_detail()
    put(conn, "stores", {
        "store_code": d.get("storeNo") or store_code, "store_name": d.get("storeName"),
        "abbreviation": d.get("abbreviation"), "address": d.get("address"),
        "business_mode_cn": d.get("businessModeCn"), "image_level": d.get("globalImageLevel"),
        "home_platform": d.get("homePlatformCn"), "city_level": d.get("cityLevel"),
        "province": d.get("province"), "city": d.get("city"), "county": d.get("county"),
        "longitude": d.get("longitude"), "latitude": d.get("latitude"),
        "working_hours": d.get("workingHours"), "receipt_name": d.get("receiptName"),
        "tax_bureau_code": d.get("taxBureauCode"), "org_name": d.get("orgName"),
        "org_path": d.get("orgPath"), "supplier_name": d.get("serviceSupplierName"),
        "customer_name": d.get("customerName"), "raw": jd(d)})
    return d


def load_medias(conn, sess: CbgSession, store_code: str) -> int:
    """收款方式字典 —— ⚠ 信封特殊：直接返回 {"mediaMemberList":[...]}，没有 status/result，
    所以**不能**走 CbgClient._request（会判成失败），只能裸调。

    ⚠ 分页一样要翻、要断言 —— 它和别的列表接口一样带 `pageVO`。
    """
    def _req(p):
        r = requests.post(CBG_BASE + MEDIA_PATH,
                          params={"locale": "zh_CN", "curPage": p["curPage"],
                                  "pageSize": p["pageSize"]},
                          json={"isParmas": {"curPage": p["curPage"], "pageSize": p["pageSize"]},
                                "storeCode": store_code, "terminalGroupCode": "",
                                "isActive": 1, "language": "Cn", "timezone": "Asia/Shanghai"},
                          headers=sess.headers(), timeout=40)
        return r.json() or {}

    rows = list_all_pages(_req, {}, envelope="mediaMemberList", label="收款方式字典")
    for m in rows:
        put(conn, "payment_medias", {
            "media_no": str(m.get("mediaNo")), "media_member_no": str(m.get("mediaMemberNo")),
            "media_name": m.get("mediaName"), "media_desc": m.get("mediaDesc"),
            "pay_channel_name": m.get("payChannelName"), "currency": m.get("currency"),
            "is_active": m.get("isActive"), "is_broker": m.get("isBroker"),
            "open_cash_drawer": m.get("openCashDrawer"),
            "declaration_required": m.get("declarationRequired"),
            "invoice_allowed": m.get("invoiceAllowed"), "raw": jd(m)})
    return len(rows)


def list_all_orders(client: CbgClient, start: int, end: int, page_size: int = 200) -> list:
    """把窗口内的销售单**取全**。

    ⚠⚠ **不要传 `returnStatus=0`**（`CbgClient.list_orders` 的默认值就是 0）：
    实测它会把「**已退货 / 已关闭**」的原始销售单整张过滤掉 ——
    去掉它：**168 张 → 172 张**，多出来的 4 张正好是 4 张退货单对应的原单
    （`status=3 已关闭`、`returnStatus=1`、金额 1488/6300/6999/6999）。
    漏了这 4 张，退货就成了「有退无销」的孤儿单。
    ⚠ 同理不传 `payStatus`（本店两种取法都是 172 张，但别替门店假设没有待支付单）。

    `start/end` 给 `None` = **不传时间窗 = 取全部历史**（447 单 / 83 天实测）。
    翻页和"抓全了没有"由 `list_all_pages` 负责 —— 447 单 = 3 页，
    **翻页一断就是静默拿到 200 张**。
    """
    body = {"ean": "", "historyData": False, "bussinessTypes": [],
            "language": "Cn", "timezone": "Asia/Shanghai"}
    time_filter(body, start, end)
    if client.store_code:
        body["storeCode"] = client.store_code
    return list_all_pages(lambda p: client._request("POST", LIST_PATH, payload=p),
                          body, page_size=page_size, label="销售单列表")


def load_orders(conn, client: CbgClient, store_code: str, start: int, end: int,
                verbose: bool = True, page_size: int = 200, orders=None) -> dict:
    # `orders` 已经取好就不要再请求一次（`main` 为了定年份会先取一遍）
    if orders is None:
        orders = list_all_orders(client, start, end, page_size=page_size)
    stat = {"orders": len(orders), "lines": 0, "payments": 0, "errors": 0,
            "no_sn_orders": 0, "pay_mismatch": 0, "returned": 0}
    if verbose:
        print("订单 %d 张，逐单取详情…" % len(orders), flush=True)

    for i, o in enumerate(orders, 1):
        dn = o.get("documentNo") or ""
        try:
            body = {"documentNo": dn, "historyData": False, "language": "Cn",
                    "timezone": "Asia/Shanghai"}
            if store_code:
                body["storeCode"] = store_code
            r = client._request("POST", DETAIL_PATH, payload=body).get("result") or {}
        except (CbgError, CbgAuthError) as e:
            stat["errors"] += 1
            print("  [%d/%d] %s 详情失败：%s" % (i, len(orders), dn, e), flush=True)
            r = {}

        lines = r.get("details") or o.get("details") or []
        sn_lines = [ln for ln in lines if (ln.get("sn") or "").strip()]
        if not sn_lines:
            stat["no_sn_orders"] += 1
        if (o.get("returnStatus") or 0):
            stat["returned"] += 1          # 已退货/已关闭的原单（现在也收进来了）

        conn.execute("DELETE FROM order_lines WHERE document_no = ?", (dn,))
        for ln in lines:
            # ⭐ 通用提取：**明细行每个字段都提成列**，不再手写映射
            put(conn, "order_lines", row_from(ln, skip=(), extras={
                "document_no": dn,
                "line_no": ln.get("lineNo") or 0,
                "sn": (ln.get("sn") or "").strip(),
            }))
            stat["lines"] += 1

        # 支付：详情的字段更全；**但 remark 只有列表投影里有**（详情的 remark 是 null）
        list_pay = {p.get("paymentNo"): p for p in (o.get("payments") or [])}
        detail_pay = r.get("payments") or []
        if list_pay and len(detail_pay) != len(list_pay):
            stat["pay_mismatch"] += 1
        conn.execute("DELETE FROM payments WHERE document_no = ?", (dn,))
        for p in (detail_pay or list(o.get("payments") or [])):
            no = p.get("paymentNo") or "%s-%s" % (p.get("mediaNo"), p.get("paymentAmount"))
            lp = list_pay.get(no) or {}
            put(conn, "payments", row_from(p, skip=(), extras={
                "document_no": dn, "payment_no": no,
                "media_no": str(p.get("mediaNo") or ""),
                "media_member_no": str(p.get("mediaMemberNo") or ""),
                "payment_time": ts2str(p.get("paymentTime")),
                "payment_ts": p.get("paymentTime"),
                "remark": lp.get("remark"), "source": "detail" if detail_pay else "list",
                "raw": jd(p)}))
            stat["payments"] += 1

        # 业务标记：标签 ∪ 备注（实测两者会不一致，10 单实例）
        conn.execute("DELETE FROM order_labels WHERE document_no = ?", (dn,))
        labels = [(k, v) for k, v in
                  (("orderLabelList", x) for x in (o.get("orderLabelList") or []) if x)]
        for k, key in (("remark", "remark"), ("stateLabel", "stateLabel"),
                       ("memberLabel", "memberLabel"), ("neuLabel", "neuLabel")):
            v = (r.get(key) if k != "remark" else o.get("remark")) or ""
            if v:
                labels.append((k, v))
        for src, val in labels:
            put(conn, "order_labels",
                {"document_no": dn, "source": src, "label": str(val)[:200]})

        # ⭐ 订单主体：**列表 ∪ 详情** 整份提成列（列表的非空值覆盖详情）
        merged = dict(r)
        merged.update({k: v for k, v in o.items() if v not in (None, "", [], {})})
        put(conn, "orders", row_from(merged, extras={
            "document_no": dn,
            "doc_create_time": ts2str(merged.get("docCreateTime")),
            "doc_create_ts": merged.get("docCreateTime"),
            "create_time": ts2str(merged.get("createTime")),
            "outbound_time": ts2str(merged.get("outboundTime")),
            "order_label_list": "|".join(o.get("orderLabelList") or []),
            "line_count": len(lines), "sn_count": len(sn_lines),
            "raw": jd({"list": o, "detail": r})}))

        if verbose and (i % 20 == 0 or i == len(orders)):
            print("  ...%d/%d（明细 %d 行 / 支付 %d 笔）"
                  % (i, len(orders), stat["lines"], stat["payments"]), flush=True)
        time.sleep(0.05)                      # 别把接口打太急
    return stat


def load_returns(conn, client: CbgClient, store_code: str, start: int, end: int,
                 page_size: int = 200) -> dict:
    """退货单 + 退货明细（**SN 在详情里**）+ 退款流水。

    ⚠ 两套接口分开：列表 `/sale-return/paged-list`（没有 SN）、
    详情 `POST /isrp/soms/sale-return`（body 只要 documentNo，**有 SN**，还给出
    `relatedDocNo` = 原销售单号）。拿退货单号去查 `/isrp/soms/sale-order` 会明确报
    「Cannot find the corresponding sales order」—— 别混用。
    """
    stat = {"returns": 0, "refunds": 0, "lines": 0, "detail_errors": 0}
    for kind, path in (("sale_return", RETURN_PATH),
                       ("reservation_return", RESV_RETURN_PATH)):
        body = {"ean": "", "historyData": False, "bussinessTypes": [],
                "language": "Cn", "timezone": "Asia/Shanghai"}
        time_filter(body, start, end)
        if store_code:
            body["storeCode"] = store_code
        try:
            rows = list_all_pages(lambda p: client._request("POST", path, payload=p),
                                  body, page_size=page_size,
                                  label="退货单列表(%s)" % kind)
        except FetchIncomplete:
            # ⚠ **抓不全必须让整个抓取失败**，不能被下面那句 continue 吞掉 ——
            #   吞掉就又回到"报成功、其实缺行"的老路上。
            raise
        except (CbgError, CbgAuthError) as e:
            print("  %s 取失败：%s" % (kind, e), flush=True)
            continue
        for d in rows:
            dn = d.get("documentNo") or ""

            # —— 详情（只有销售退货试过；预订退货的详情接口没验）—— #
            det = {}
            if kind == "sale_return":
                dbody = {"documentNo": dn, "historyData": False, "language": "Cn",
                         "timezone": "Asia/Shanghai"}
                if store_code:
                    dbody["storeCode"] = store_code
                try:
                    det = client._request("POST", RETURN_DETAIL_PATH,
                                          payload=dbody).get("result") or {}
                except (CbgError, CbgAuthError) as e:
                    stat["detail_errors"] += 1
                    print("  %s 详情失败：%s" % (dn, e), flush=True)
                conn.execute("DELETE FROM return_lines WHERE kind = ? AND document_no = ?",
                             (kind, dn))
                for ln in (det.get("details") or []):
                    put(conn, "return_lines", {
                        "kind": kind, "document_no": dn,
                        "line_no": ln.get("lineNo") or 0,
                        "sn": (ln.get("sn") or "").strip(), "ean": ln.get("ean"),
                        "sku": ln.get("sku"), "spu": ln.get("spu"), "bpart": ln.get("bpart"),
                        "category_id": ln.get("categoryId"), "item_name": ln.get("itemName"),
                        "specification": ln.get("specification"),
                        "quantity": ln.get("quantity"), "unit": ln.get("unit"),
                        "unit_price": ln.get("unitPrice"),
                        "included_tax_amount": ln.get("includedTaxAmount"),
                        "excluded_tax_amount": ln.get("excludedTaxAmount"),
                        "tax_amount": ln.get("taxAmount"), "tax_rate": ln.get("taxRate"),
                        "verifiable_amount": ln.get("verifiableAmount"),
                        "non_refundable_amount": ln.get("nonRefundableAmount"),
                        "return_discount_amount": ln.get("returnDiscountAmount"),
                        "deposit_amount": ln.get("depositAmount"),
                        "service_goods": ln.get("serviceGoods"),
                        "saleable": ln.get("saleable"),
                        "inventory_status": ln.get("inventoryStatus"),
                        "related_doc_no": ln.get("relatedDocNo"),
                        "related_doc_line_no": ln.get("relatedDocLineNo"),
                        "first_doc_no": ln.get("firstDocNo"),
                        "first_doc_line_no": ln.get("firstDocLineNo")})
                    stat["lines"] += 1

            put(conn, "returns", {
                "kind": kind, "document_no": dn, "order_no": d.get("orderNo"),
                "store_code": d.get("storeCode"), "store_name": d.get("storeName"),
                "pos_id": d.get("posId"), "device_no": d.get("deviceNo"),
                "document_source": d.get("documentSource"), "status": d.get("status"),
                "pay_status": d.get("payStatus"),
                "enterprise_pay_status": d.get("enterprisePayStatus"),
                "included_tax_amount": d.get("includedTaxAmount"),
                "excluded_tax_amount": d.get("excludedTaxAmount"),
                "tax_amount": d.get("taxAmount"), "payment_amount": d.get("paymentAmount"),
                "deposit_amount": d.get("depositAmount"),
                "return_reason": d.get("returnReason"),
                "return_reason_name": d.get("returnReasonName"),
                "cashier_id": d.get("cashierId"),
                "sales_assistant_id": d.get("salesAssistantId"),
                "doc_create_time": ts2str(d.get("docCreateTime")),
                "doc_create_ts": d.get("docCreateTime"),
                "business_mode_code": d.get("businessModeCode"),
                "business_mode_cn": d.get("businessModeCn"), "member_id": d.get("memberId"),
                # 详情才有的
                "related_doc_no": det.get("relatedDocNo"),
                "in_bound_status": det.get("inBoundStatus"),
                "sales_assistant_name": det.get("salesAssistantName"),
                "scenario_type": det.get("scenarioType"),
                "order_business_type": det.get("orderBusinessType"),
                "hw_user_id": det.get("hwUserId"), "user_hash": det.get("userHash"),
                "point_value": det.get("pointValue"),
                "reclaim_point_amount": det.get("reclaimPointAmount"),
                "raw": jd({"list": d, "detail": det})})
            stat["returns"] += 1

            conn.execute("DELETE FROM return_refunds WHERE kind = ? AND document_no = ?",
                         (kind, dn))
            for f in (d.get("refunds") or det.get("refunds") or []):
                put(conn, "return_refunds", {
                    "kind": kind, "document_no": dn,
                    "refund_no": f.get("refundNo") or f.get("paymentNo") or "?",
                    "payment_no": f.get("paymentNo"), "refund_amount": f.get("refundAmount"),
                    "refund_time": ts2str(f.get("refundTime")),
                    "media_no": str(f.get("mediaNo") or ""),
                    "media_member_no": str(f.get("mediaMemberNo") or ""),
                    "media_name": f.get("mediaName"), "media_desc": f.get("mediaDesc"),
                    "status": f.get("status"),
                    "nat_subsidy_discount_amount": f.get("natSubsidyDiscountAmount"),
                    "raw": jd(f)})
                stat["refunds"] += 1
    return stat


# --------------------------------------------------------------------- 主流程
def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="把 CBG 能抓到的数据全部落进 SQLite")
    ap.add_argument("--root", default="")
    ap.add_argument("--session", default=".secrets/cbg-default.json")
    ap.add_argument("--store-code", default="")
    ap.add_argument("--days", type=int, default=30)
    ap.add_argument("--month", default="",
                    help="抓某月（YYYY-MM）；给 current 或不给=当月。**日常流程用这个**")
    ap.add_argument("--year", type=int, default=0,
                    help="抓这一年 → out/cbg-<年>.db（一年一个库）")
    ap.add_argument("--all", action="store_true",
                    help="不传时间窗 = 抓该店**全部历史**（实测 447 单 / 83 天）")
    ap.add_argument("--page-size", type=int, default=200)
    ap.add_argument("--db", default="",
                    help="显式指定库文件；不给就按年份自动取 out/cbg-<年>.db")
    ap.add_argument("--skip-returns", action="store_true")
    ap.add_argument("--skip-medias", action="store_true")
    ap.add_argument("-q", "--quiet", action="store_true")
    args = ap.parse_args(argv)

    sess_path = Path(args.session)
    if not sess_path.is_absolute():
        sess_path = ROOT / sess_path
    if not sess_path.is_file():
        # ⚠ 返回码而不是 `raise SystemExit` —— 本函数会被**进程内**调用
        #   （`cli.cmd_dump` → `run_daily`），SystemExit 是 BaseException，
        #   会**穿过**调用方的 `if rc != 0` 直接把进程带走，退出码约定失效。
        print("没有会话文件：%s\n  先跑：python grab.py --store-code <码> auth" % sess_path,
              file=sys.stderr)
        return 1
    sess = CbgSession.load(sess_path)

    client = CbgClient(sess, store_code=args.store_code or None, timeout=40,
                       verbose=not args.quiet)
    ok, why = client.ping()
    print("会话自检：%s %s" % ("✅" if ok else "❌", why), flush=True)
    if not ok:
        return 1

    # ------------------------------------------------------------------ 定窗口
    if args.month:
        ym = this_month() if args.month == "current" else args.month
        start, end = month_range(ym)
        win_txt = "%s（%s ~ %s）" % (ym, ts2str(start), ts2str(end))
    elif args.year:
        start, end = year_range(args.year)
        win_txt = "%d 年（%s ~ %s）" % (args.year, ts2str(start), ts2str(end))
    elif args.all:
        start = end = None
        win_txt = "全部历史（不传时间窗）"
    else:
        start, end = range_ts(args.days)
        win_txt = "%s ~ %s（近 %d 天）" % (ts2str(start), ts2str(end), args.days)
    started = datetime.datetime.now(CST).strftime("%Y-%m-%d %H:%M:%S")
    print("窗口：%s" % win_txt, flush=True)

    # ⚠ 先把订单列表取回来 —— `--all` 时要靠它才知道数据落在哪一年。
    #   多取这一次不会白费：下面 `load_orders` 直接用它，不再重复请求。
    orders = list_all_orders(client, start, end, page_size=args.page_size)

    # -------------------------------------------------------------------- 定库
    if args.db:
        db = Path(args.db)
    else:
        years = sorted({ts_year(o["docCreateTime"]) for o in orders if o.get("docCreateTime")})
        want = (args.year
                or (int(args.month[:4]) if args.month and args.month != "current" else 0)
                or (years[0] if len(years) == 1 else 0))
        if not want:
            # ⚠ 同上：返回码，不要 SystemExit（会被进程内调用方穿过）
            print("⚠ 这次抓到的数据跨了 %s 年 —— **一年一个库**，请按年分次抓：\n%s"
                  % ("、".join(str(y) for y in years) or "?",
                     "\n".join("   python dump.py --year %d --store-code %s"
                               % (y, args.store_code or "<门店码>") for y in years)),
                  file=sys.stderr)
            return 1
        db = year_db(ROOT / "out", want)
    if not db.is_absolute():
        db = ROOT / db
    db.parent.mkdir(parents=True, exist_ok=True)
    print("库：%s" % db, flush=True)

    conn = connect(db)                 # ⚠ 别直接 sqlite3.connect —— 见 connect 的说明
    conn.executescript(SCHEMA)
    _ensure_columns(conn)
    try:
        d = load_store(conn, client, args.store_code)
        print("门店：%s %s" % (d.get("storeNo"), d.get("storeName")), flush=True)

        media_n = 0
        if not args.skip_medias:
            media_n = load_medias(conn, sess, args.store_code)
            print("收款方式字典：%d 种" % media_n, flush=True)

        st = load_orders(conn, client, args.store_code, start, end,
                         verbose=not args.quiet, page_size=args.page_size, orders=orders)
        print("订单 %d 张（其中已退货/已关闭 %d） / 明细 %d 行 / 支付 %d 笔"
              "（无 SN 的订单 %d、支付条数不一致 %d、失败 %d）"
              % (st["orders"], st["returned"], st["lines"], st["payments"],
                 st["no_sn_orders"], st["pay_mismatch"], st["errors"]), flush=True)

        rs = {"returns": 0, "refunds": 0, "lines": 0, "detail_errors": 0}
        if not args.skip_returns:
            rs = load_returns(conn, client, args.store_code, start, end,
                              page_size=args.page_size)
            print("退货单 %d 张 / 退货明细 %d 行（带 SN）/ 退款流水 %d 笔（详情失败 %d）"
                  % (rs["returns"], rs["lines"], rs["refunds"], rs["detail_errors"]),
                  flush=True)

        finished = datetime.datetime.now(CST).strftime("%Y-%m-%d %H:%M:%S")
        put(conn, "meta", {"key": "built_at", "value": finished})
        put(conn, "meta", {"key": "store_code", "value": args.store_code or ""})
        put(conn, "meta", {"key": "window", "value": win_txt})
        put(conn, "meta", {"key": "all_history", "value": "1" if args.all else "0"})
        put(conn, "meta", {"key": "days", "value": str(args.days)})
        put(conn, "meta", {"key": "schema", "value": "1"})
        conn.execute(
            "INSERT INTO fetch_log (started_at, finished_at, days, window_start, window_end,"
            " store_code, orders, order_lines, payments, returns, refunds, errors, note)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (started, finished, args.days,
             ts2str(start) if start else "", ts2str(end) if end else "", args.store_code,
             st["orders"], st["lines"], st["payments"], rs["returns"], rs["refunds"],
             st["errors"], "medias=%d all=%d" % (media_n, 1 if args.all else 0)))
        conn.commit()

        # —— 抓完自证：**不等就非 0 退出**，不能只打印 —— #
        chk = selfcheck(conn)
        print("\n=== 抓完自证 ===", flush=True)
        for line in chk["lines"]:
            print("  " + line, flush=True)
        print("  " + ("✅ 通过" if chk["ok"] else "❌ 没过 —— 这份库不完整，别拿它算合规"),
              flush=True)

        print_summary(conn, db)
    finally:
        conn.close()
    return 0 if chk["ok"] else 2


def print_summary(conn, db=None) -> None:
    """抓完把库里有什么**摊开给人看**。

    ⚠ 单独一个函数是为了**能测**。以前这段是 `main` 里一长串 `print`，
    只有真去华为抓一次才会跑到 —— 于是它坏了也没人知道。
    **门店那次就是这么炸的**：给主连接设了 `sqlite3.Row` 之后，
    `"%-9s %4d" % row` 当场 `TypeError`（Python 的 `%` 只对**元组**展开，
    非元组只允许一个占位符）—— 而数据其实**已经写进库了**，
    崩的只是最后的打印。退出码 9 ⇒ 整条日常流程中止 ⇒ 报量排查和 POS 全没跑。
    **最坏的一种失败：活儿干完了，工具说自己失败了。**

    所以这里两件事一起做：① 抽出来让它可以被测；② 每处都写 `tuple(row)`，
    **不依赖连接的行类型**（谁哪天换了 `row_factory` 都不会再炸）。
    """
    if db is not None:
        print("\n库：%s（%.1f KB）" % (db, db.stat().st_size / 1024.0), flush=True)
    for label, sql in (
        ("订单数 / 金额（订单级，不会重复）",
         "SELECT COUNT(*), ROUND(SUM(included_tax_amount),2) FROM orders"),
        ("明细行 / 有 SN 的行", "SELECT COUNT(*), SUM(sn <> '') FROM order_lines"),
        ("唯一 SN 数", "SELECT COUNT(DISTINCT sn) FROM order_lines WHERE sn <> ''"),
        ("支付笔数 / 金额", "SELECT COUNT(*), ROUND(SUM(payment_amount),2) FROM payments"),
        ("业务标记数", "SELECT COUNT(*) FROM order_labels"),
        ("退货单 / 退款", "SELECT (SELECT COUNT(*) FROM returns),"
                      " (SELECT COUNT(*) FROM return_refunds)"),
        ("退货明细行 / 有 SN", "SELECT COUNT(*), SUM(sn <> '') FROM return_lines"),
        ("收款方式", "SELECT COUNT(*) FROM payment_medias"),
    ):
        print("  %-34s %s" % (label, tuple(conn.execute(sql).fetchone())), flush=True)

    print("\n按 POS/设备（v_pos_usage）：", flush=True)
    for row in conn.execute("SELECT pos_id, device_no, document_source, orders, ROUND(amount,2)"
                            " FROM v_pos_usage"):
        print("   %-9s %-9s src=%-3s %4d 单  %12.2f" % tuple(row), flush=True)
    print("\n按支付方式（v_payment_summary）：", flush=True)
    for row in conn.execute("SELECT media_name, media_no, media_member_no, payments,"
                            " ROUND(amount,2) FROM v_payment_summary LIMIT 10"):
        print("   %-14s %-3s/%-3s %4d 笔  %12.2f" % tuple(row), flush=True)
    print("\n业务标记（v_label_summary）：", flush=True)
    for row in conn.execute("SELECT source, label, orders FROM v_label_summary LIMIT 12"):
        print("   %-16s %-22s %d 单" % tuple(row), flush=True)
    print("\n退货单 + 退的 SN / 原单（v_return_with_sn）：", flush=True)
    for row in conn.execute(
            "SELECT document_no, ROUND(amount,2), sn, related_doc_no, device_no"
            " FROM v_return_with_sn ORDER BY doc_create_time"):
        print("   %s  %9.2f  SN=%-18s 原单=%s  %s" % tuple(row), flush=True)


if __name__ == "__main__":
    sys.exit(main())
