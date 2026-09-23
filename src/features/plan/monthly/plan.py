# -*- coding: utf-8 -*-
"""月度生意计划的 **IO 层** —— 读名单、查库、落盘。

口径在 `metric.py`（纯函数），这儿只管"东西从哪来、往哪去"。
分两个文件是**唯一能让口径脱离数据库单测**的办法（照 `pos_metric`/`pos_report` 那套）。

## 一步一步

```
config/stores.yaml  ── 名单内**有 erp_name + region** 的 28 家（§三·0）
        ↓
   erp_sales（池C）── 两段各查一次：当月至今 / 上月同期
        ↓  剔除：非白名单单据 · 演示机体验机 · 外调 · **名单外的店**
   metric.collect() → 四层累加表
        ↓  metric.tree() / metric.summary()
   out/plan-<年>.json
```

## ⚠⚠ 三条要守住

1. **只算名单内的店**。云商那份导出有 43 家，多的是**联想专卖店 / 部门 / 机场店** ——
   用户 2026-09-21 原话：「问题是我这 28 家店里没联想」。
   ⚠ 名单外的行**不进任何计算**，但要**计数报出来**（静默丢掉 = 数字对不上又找不到人）。
2. **落盘存全部 28 家**，按身份的过滤放在**接口层**（`web.role_scope()`）——
   落盘里只留本店的话，区长/平台那份还得再算一次（照 `attain` 的分工）。
3. **上层由下层加总**（`metric.rollup`），不许另查一次库 ——
   两次数出来的东西对不上，是这类看板最经典的"总额 ≠ 明细之和"。
"""

from __future__ import annotations

import datetime
import json
import sqlite3
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from . import metric as M

#: 落盘文件名（一年一个，只存"最近算出来的那一期"）
FILE_FMT = "plan-%d.json"

#: 一次取数要的列。
#:
#: ⚠ 一次全查（别按列查）：`erp_sales` 上只有 `制单时间` 的索引，
#:   按 `支付时间` 过滤是**全表扫**，查几遍就扫几遍。
#: ⚠ 末尾那个 `店员` 是 2026-09-21 加的（用户：「门店名点开可以**拆分到人**」）——
#:   加列**只能往后加**：`load_sales` 是按位置解包的，插在中间会让金额跑到"商品名称"上，
#:   而且**不会报错**（都是字符串/数字，看着还挺合理）。
#: 一定要有的那几列。⚠ **加列只能往后加**：`load_sales` 是按位置解包的，
#:   插在中间会让金额跑到"商品名称"上，而且**不会报错**（都是字符串/数字，看着还挺合理）。
COLS = ("门店", "一级分类", "二级分类", "三级分类", "数量", "金额", "零售考核毛利",
        "单据类型", "商品名称", "串号标识")

#: 2026-09-21 加的（用户：「门店名点开可以**拆分到人**」）—— 但它**可能不存在**：
#: `erp_sales` 的列是 `ensure_columns()` 按云商接口返回**动态建的**，
#: 老库（当年那几次导出里没有 `HandlerName`）就没有这一列 ⇒ 直接 `SELECT 店员`
#: 会让**整个 plan 步骤**报"no such column"，而这一页只是少个"拆到人"。
#: ⇒ 先 `PRAGMA` 问一下，没有就补一列空串（`who` 全空 = 那一层没数据，页面不显示人）。
OPT_COLS = ("店员",)

#: 兜底用（列齐全时就是它）—— 保留模块级这个名字，免得别处 `P.SQL` 找不到
SQL = ("SELECT %s FROM erp_sales WHERE 支付时间 >= ? AND 支付时间 <= ?"
       % ", ".join(COLS + OPT_COLS))


def sales_sql(conn: sqlite3.Connection) -> str:
    """按这张库**实际有的列**拼出来（见 `OPT_COLS` 那段）。"""
    try:
        have = {str(r[1]) for r in conn.execute("PRAGMA table_info(erp_sales)")}
    except sqlite3.Error:
        return SQL
    cols = list(COLS) + [c if c in have else "'' AS %s" % c for c in OPT_COLS]
    return "SELECT %s FROM erp_sales WHERE 支付时间 >= ? AND 支付时间 <= ?" % ", ".join(cols)

#: 体检用：这段期间在库里覆盖了几个自然日（算环比前看上月够不够）
SQL_DAYS = ("SELECT COUNT(DISTINCT substr(支付时间, 1, 10)) FROM erp_sales "
            "WHERE 支付时间 >= ? AND 支付时间 <= ?")


class PlanError(RuntimeError):
    """取数/算数的问题。**别用 SystemExit** —— 本模块会被进程内调用（AGENTS.md 坑 11）。"""


# ------------------------------------------------------------------ 名单
#: 区域展示顺序 —— 跟增值防护膜 `film.export.REGION_ORDER` 同一份口径
#: （film 那边不能反向 import：`film.compute` 已经依赖本模块，会成环）。
REGION_ORDER = ("西北区", "市区", "南区", "服务站", "北区", "西区")


def stores_in_scope(root) -> List[str]:
    """名单内**参与统计**的云商门店名（有 `erp_name` **且**有 `region` 的那些）。

    实测 2026-09-21：`stores.yaml` 30 条里正好 **28 条**是店
    （15 体验店 + 13 合作店；市区 14 / 西北区 8 / 南区 6）。
    另两条**不是店**：`平台岗`（虚拟，没 region）和一条空名 —— 天然被这个条件排掉。

    ⚠ 2026-09-22 用户：「**也按区域排好序**」——
      按 `REGION_ORDER` 分组（跟 film 一致），**区内仍照名单顺序**。
      原来"顺序照名单不排序"在名单跨区穿插时会对不齐。
    """
    from .... import config_io
    from ....paths import ROOT
    ranked: List[Tuple[int, int, str]] = []   # (区域序, 名单序, 店名)
    for i, s in enumerate(config_io.stores_table(root or ROOT)):
        name = str(s.get("erp_name") or "").strip()
        region = str(s.get("region") or "").strip()
        if not (name and region):
            continue
        rank = REGION_ORDER.index(region) if region in REGION_ORDER else len(REGION_ORDER)
        ranked.append((rank, i, name))
    ranked.sort()
    out: List[str] = []
    for _, _, name in ranked:
        if name not in out:
            out.append(name)
    return out


def stores_by_region(root) -> Dict[str, str]:
    """`{云商门店名: 区域}` —— 按区域分组显示时用（导出的"区域"列）。"""
    from .... import config_io
    from ....paths import ROOT
    out: Dict[str, str] = {}
    for s in config_io.stores_table(root or ROOT):
        name = str(s.get("erp_name") or "").strip()
        region = str(s.get("region") or "").strip()
        if name and region:
            out.setdefault(name, region)
    return out


# ------------------------------------------------------------------ 取销售
def load_sales(conn: sqlite3.Connection, start, end,
               stores: Optional[List[str]] = None) -> Tuple[List[M.Sale], dict]:
    """期间内的销售行 → `(sales, dropped)`。

    `dropped` 是**剔除统计**（`{"演示机/体验机": n, "外调": n, "单据类型不计入": n,
    "名单外的店": n}`）—— ⚠ 静默剔除是查不出来的（`pools` 那边吃过"数字对不上
    又找不到人"的亏），所以每一条都要能报出来。

    ⚠ 剔除放在 **Python 侧**（不在 SQL 里 `NOT LIKE`）：
      ① SQL 里剔了就**数不着**了，而"剔了几行"要能报出来；
      ② `LIKE` 会让本来就没索引的全表扫再来一遍。
    ⚠ 期间**左闭右闭**（`<= end`），`end` 是那天的日期、补到 23:59:59。
    """
    lo = "%s 00:00:00" % start
    hi = "%s 23:59:59" % end
    scope = set(stores) if stores is not None else None
    dropped: Dict[str, int] = {}
    sales: List[M.Sale] = []
    try:
        rows = conn.execute(sales_sql(conn), (lo, hi))
    except sqlite3.Error as e:                                 # 表还没建（库是空的）
        raise PlanError("查 erp_sales 失败：%s" % e)
    for store, c1, c2, c3, qty, amount, profit, kind, product, mark, who in rows:
        name = str(store or "").strip()
        if scope is not None and name not in scope:
            # ⚠ 名单外的店（联想专卖店 / 部门 / 机场店…）—— 一条都不进计算
            _bump(dropped, "名单外的店")
            continue
        why = M.drop_reason(kind, product, mark)
        if why:
            _bump(dropped, why)
            continue
        sales.append(M.Sale(
            store=name,
            cat1=str(c1 or "").strip(), cat2=str(c2 or "").strip(), cat3=str(c3 or "").strip(),
            qty=M.qty_of(qty), amount=M.money_of(amount), profit=M.money_of(profit),
            who=str(who or "").strip()))
    return sales, dropped


def _bump(counter: dict, key: str) -> None:
    counter[key] = counter.get(key, 0) + 1


def data_as_of(conn: sqlite3.Connection, start, end) -> str:
    """期间内 `MAX(支付时间)` 的**日期部分**。

    ⚠ 用**库里的实际最大时间**，不是"今天"：抓数断了三天时，写"截至 9/18"
      而不是"今天" —— 否则页面上的数看着像"这三天没卖"，而真相是"这三天没抓"。
    ⚠ 全区分一个（不分店）：某店这几天没卖就成空字符串，界面上会显示成"没有数据"。
    """
    lo, hi = "%s 00:00:00" % start, "%s 23:59:59" % end
    try:
        row = conn.execute("SELECT MAX(支付时间) FROM erp_sales "
                           "WHERE 支付时间 >= ? AND 支付时间 <= ?", (lo, hi)).fetchone()
    except sqlite3.Error:
        return ""
    return str((row or [""])[0] or "")[:10]


def covered_days(conn: sqlite3.Connection, span) -> int:
    """这段期间在库里覆盖了几个自然日（**体检上月够不够算环比**）。"""
    lo, hi = "%s 00:00:00" % span.start, "%s 23:59:59" % span.end
    try:
        row = conn.execute(SQL_DAYS, (lo, hi)).fetchone()
    except sqlite3.Error:
        return 0
    return int((row or [0])[0] or 0)


# ------------------------------------------------------------------ 算
def compute(conn: sqlite3.Connection, stores: List[str], day=None, root=None) -> dict:
    """查两段 → 算三层树 → 落盘字典（形状 = `/api/plan` 的返回）。

    ⚠ **不做本店过滤**（那是接口层的事）—— 落盘里要是只有本店，
      区长/平台那份还得再算一次（照 `attain.compute` 的分工）。
    """
    today = M._as_date(day or datetime.date.today())
    cur_span = M.month_span(today)
    prev_span = M.prev_month_same_span(today)

    cur_sales, cur_drop = load_sales(conn, cur_span.start, cur_span.end, stores)
    prev_sales, prev_drop = load_sales(conn, prev_span.start, prev_span.end, stores)

    cur_acc, cur_unk, cur_skip = M.collect(cur_sales)
    prev_acc, prev_unk, prev_skip = M.collect(prev_sales)
    rows = M.tree(cur_acc, prev_acc, stores)
    # ⭐ **点门店拆到人**（用户 2026-09-21）—— 带人那一层的累加表**另收一份**。
    #   ⚠ 用同一个 `collect`（同一套过滤/映射）⇒ "人的数加起来 = 店里那一行"恒成立。
    #   ⚠ 它**不进** `summary`/`合计`：那是门店级的口径，把人加进去就重复计了。
    cur_who, _u1, _s1 = M.collect(cur_sales, with_who=True)
    prev_who, _u2, _s2 = M.collect(prev_sales, with_who=True)
    people = M.people_of(cur_who, prev_who, stores)
    for r in rows:
        r["people"] = people.get(r["store"]) or []

    dropped: Dict[str, int] = {}
    for d in (cur_drop, prev_drop):
        for k, v in d.items():
            dropped[k] = dropped.get(k, 0) + v

    warnings = []
    as_of = data_as_of(conn, cur_span.start, cur_span.end)
    if not as_of:
        warnings.append("库里这段期间一行销售都没有 —— 先确认抓数跑过（erp-dump）")
    else:
        gap = (cur_span.end - M._as_date(as_of)).days
        if gap > 0:
            warnings.append("本月数据只到 %s（今天 %s）—— 后面 %d 天还没抓进来"
                            % (as_of, today.isoformat(), gap))
    miss = M.missing_months(covered_days(conn, prev_span), prev_span)
    if miss:
        warnings.append(miss)
    regions = stores_by_region(root) if root else {}
    return {
        "exists": True,
        "period": {"cur": cur_span.as_dict(), "prev": prev_span.as_dict(),
                   "today": today.isoformat()},
        "data_as_of": as_of,
        "computed_at": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "blocks": list(M.BLOCKS),
        "stores": list(stores),
        #: `{门店: 区域}` —— 导出件的「区域」列 + 界面按区分组（不是算出来的，是名单里的）
        "regions": regions,
        "rows": rows,
        # ⚠ 合计**从同一棵树重算**（不是另留一份累加）—— 接口层按身份滤掉几家之后
        #   还要再调一次 `M.summarize(过滤后的 rows)`，见 `web.App.plan`。
        "summary": M.summarize(rows),
        # 分区汇总「共计」行 —— 后端算好再下发（照 film）；接口层滤完店还会重算
        "region_sums": M.region_sums(rows, regions),
        "dropped": dropped,
        "unknown": cur_unk,
        "unknown_prev": prev_unk,
        "skipped": cur_skip,
        "skipped_prev": prev_skip,
        "warnings": warnings,
        "counts": {"cur_rows": len(cur_sales), "prev_rows": len(prev_sales)},
    }


# ------------------------------------------------------------------ 落盘
def save(root, payload: dict, year: Optional[int] = None) -> Path:
    """落 `out/plan-<年>.json` —— **先写临时文件再 rename**（照 `attain.save`）。

    ⚠ 直接覆盖写的话，中途断电 / 被杀就是一个半截 JSON，
      而前端读它时只会说"读不出来"（跟"还没算过"长得一样）。
    """
    root = Path(root)
    out = root / "out"
    out.mkdir(parents=True, exist_ok=True)
    head = str((payload.get("period") or {}).get("today") or "")[:4]
    y = year or (int(head) if head.isdigit() else datetime.date.today().year)
    path = out / (FILE_FMT % y)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(path)
    return path


def load(root, year: Optional[int] = None) -> dict:
    """读回最新算出来的那一期（`/api/plan` 用的是这个）。**读不到给 `exists: False`。**"""
    root = Path(root)
    y = year or datetime.date.today().year
    p = root / "out" / (FILE_FMT % y)
    if not p.is_file():
        cands = sorted((root / "out").glob("plan-*.json"))
        if not cands:
            return {"exists": False,
                    "error": "还没算过 —— 先跑一次 daily（或等定时器到点）"}
        p = cands[-1]
    try:
        d = json.loads(p.read_text(encoding="utf-8"))
    except Exception as e:                                     # noqa: BLE001
        return {"exists": False, "error": "落盘文件读不出来（%s）：%s" % (p.name, e)}
    d["exists"] = True
    d["file"] = p.name
    return d


def find_db(root) -> Optional[Path]:
    """订单库 —— 跟 POS / 达成那条链**同一个口径**（`app/data_state` 说了算）。"""
    from ....app import data_state
    found = data_state._find_db(Path(root))
    return Path(found) if found else None


# ------------------------------------------------------------------ 执行入口
def run(*, db: str = "", root=None, day=None, emit=None) -> dict:
    """**算一次**：读名单 → 查库 → 算 → 落盘。返回具名结果（**不抛**）。"""
    from ....paths import ROOT
    say = emit or (lambda _s: None)
    root = Path(root or ROOT)
    stores = stores_in_scope(root)
    if not stores:
        return {"ok": False, "why": "门店名单读不出来（config/stores.yaml 里没有"
                                    "带 erp_name + region 的店）"}
    path = Path(db) if db else find_db(root)
    if not path or not path.is_file():
        return {"ok": False, "why": "没找到订单库（out/cbg-<年>.db）—— 先跑一次 erp-dump"}
    conn = None
    try:
        conn = sqlite3.connect("file:%s?mode=ro" % path, uri=True)
        payload = compute(conn, stores, day=day, root=root)
    except PlanError as e:
        return {"ok": False, "why": str(e)}
    except sqlite3.Error as e:
        return {"ok": False, "why": "查库失败：%s" % e}
    finally:
        if conn is not None:
            conn.close()
    saved = save(root, payload)
    say("月度生意计划 %s ~ %s（对比 %s ~ %s）→ %d 家店"
        % (payload["period"]["cur"]["start"], payload["period"]["cur"]["end"],
           payload["period"]["prev"]["start"], payload["period"]["prev"]["end"],
           len(stores)))
    for b in M.BLOCKS:
        s = payload["summary"]["blocks"][b]
        say("  %-4s %5d 台 · %12.0f 元 · 毛利 %10.0f"
            % (b, s["cur"]["qty"], s["cur"]["amount"], s["cur"]["profit"]))
    if payload["dropped"]:
        say("  （剔除了 %s）" % "、".join("%s %d 行" % kv for kv in sorted(payload["dropped"].items())))
    for w in payload["warnings"]:
        say("  ⚠ %s" % w)
    return {"ok": True, "path": str(saved), "payload": payload,
            "why": "已算 %d 家店 · %d 行当月 / %d 行上月"
                   % (len(stores), payload["counts"]["cur_rows"], payload["counts"]["prev_rows"])}
