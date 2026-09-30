# -*- coding: utf-8 -*-
"""防护膜达成 —— **只查**本地 `erp_sales`（fetch 落的 SQLite）。

⚠ 2026-09-22（用户）：「只能从 fetch 下来的 sqlite 里取数」——
  **已去掉** `out/.sales_*.xlsx` 回落；也**不读**任何用户桌面 Excel。
  库里没有贴膜/没有库时 → `ok` 仍 True 但 note 说清「先跑抓数据」，
  不拿别的文件凑一张看起来合理的表。

窗口：本月 1 号 ～ 今天（含）。
"""

from __future__ import annotations

import datetime
import sqlite3
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from ....paths import ROOT
from ...plan.monthly.plan import stores_by_region, stores_in_scope
from . import metric
from .. import foreign as foreign_mod


def _f(x) -> float:
    try:
        return float(x or 0)
    except (TypeError, ValueError):
        return 0.0


def month_window(day=None) -> Tuple[str, str]:
    d = day or datetime.date.today()
    start = d.replace(day=1).isoformat()
    end = d.isoformat()          # 含当天
    return start, end


def time_progress(day=None) -> float:
    """时间进度 = **已过天数 ÷ 30**，封顶 100%。

    ⚠ 2026-09-29 从 `(day-1)/30` 改成 `day/30`（用户点头「问题不大」）：
      * 「截止到 28 日」= 已过 28 天 = **93.3%** —— 源表右上角实写 93.3%（= 28/30）；
      * `(day-1)/30` 到 30 号那天只有 96.7%，**永远到不了 100%**；
      * 选**上月最后一天**看全月时要 = 100%（31 号 → 31/30 封顶）。
    """
    d = day or datetime.date.today()
    return max(0.0, min(1.0, d.day / 30.0))


def find_db(root) -> Optional[Path]:
    from ....app import data_state
    found = data_state._find_db(Path(root))
    return Path(found) if found else None


#: ⚠ `店员` 可能不在老库里（跟 plan 的 `OPT_COLS` 同理）—— PRAGMA 问一下，
#:   没有就补 `'' AS 店员`，别让整页 `no such column` 挂掉。
def _select_sql(conn: sqlite3.Connection) -> str:
    base = ("SELECT 门店, 单据类型, 数量, 零售考核毛利, 一级分类, 二级分类,"
            " 商品名称, 备注, 单行备注, \"客户/顾客\", 付款方式, 支付时间")
    try:
        have = {str(r[1]) for r in conn.execute("PRAGMA table_info(erp_sales)")}
    except sqlite3.Error:
        return base + ", '' AS 店员 FROM erp_sales WHERE 支付时间 >= ? AND 支付时间 < ?"
    who = "店员" if "店员" in have else "'' AS 店员"
    return (base + ", %s FROM erp_sales WHERE 支付时间 >= ? AND 支付时间 < ?" % who)


def _load_rows_sqlite(db: Path, start: str, end: str, stores=None):
    """`erp_sales` 窗口内全部行（含无串号的贴膜/礼包）。"""
    want = set(stores) if stores is not None else None
    conn = sqlite3.connect("file:%s?mode=ro" % db, uri=True)
    try:
        conn.row_factory = sqlite3.Row
        sql = _select_sql(conn)
        end_ex = (datetime.date.fromisoformat(end)
                  + datetime.timedelta(days=1)).isoformat()
        for r in conn.execute(sql, (start, end_ex)):
            store = r["门店"] or ""
            if want is not None and store not in want:
                continue
            note = " ".join(str(r[k] or "") for k in ("备注", "单行备注",
                                                     "客户/顾客", "付款方式"))
            # 「备注点名别家门店」的**识别**只认备注/单行备注（用户口径），
            # 不能拿 note 全量判 —— 客户/付款方式里出现店名会误伤
            xnote = " ".join(str(r[k] or "") for k in ("备注", "单行备注"))
            yield {
                "店": store,
                "谁": (r["店员"] or "").strip(),
                "类型": r["单据类型"] or "",
                "名称": r["商品名称"] or "",
                "c1": r["一级分类"] or "",
                "c2": r["二级分类"] or "",
                "数量": _f(r["数量"]),
                "毛利": _f(r["零售考核毛利"]),
                "note": note,
                "xnote": xnote,
                "ts": r["支付时间"] or "",
            }
    finally:
        conn.close()


def new_bucket(c1: str, typ: str, note: str) -> str:
    """这行算不算「新机」—— `''` 不算 · `retail` 零售净 · `meituan` 美团分销净。

    ⚠ **唯一判据**：行归类看 `row_kind()`，新机那一档由这里定 ——
      `compute` 的累加和 `drill_rows()` 的明细底稿都走同一条路。
      判据写两份迟早走散（页面数字和明细合计对不上 —— 那是最贵的一种走散）。
    ⚠ `note` 必须是 `_load_rows_sqlite` 那份拼法（备注+单行备注+客户/顾客+付款方式）——
      分销判「美团」时客户字段也算（万达备注不写美团、客户字段写）。
    """
    if c1 != "手机":
        return ""
    if typ in ("零售", "零售退"):
        return "retail"
    if typ in ("分销", "分销退") and "美团" in note:
        return "meituan"
    return ""


def row_kind(c1: str, c2: str, typ: str, name: str, note: str) -> str:
    """这行属于哪个指标 —— `''` 哪个都不算 · `new_retail`/`new_meituan` · `gift` · `film`。

    ⚠ **唯一判据**：`compute` 的累加和 `drill_rows()` 的明细底稿**都走它**。
      判据写两份迟早走散（页面数字和明细合计对不上 —— 那是最贵的一种走散）。
    ⚠ 顺序跟原来一致：手机行**整行归新机**（不算新机也是 `''`，不落到礼包/贴膜分支）。
    """
    if c1 == "手机":
        b = new_bucket(c1, typ, note)
        if b == "retail":
            return "new_retail"
        if b == "meituan":
            return "new_meituan"
        return ""
    if metric.is_gift(name):
        return "gift" if typ in metric.SELL else ""
    if c2 == "贴膜" and name != metric.SOFT and typ in metric.SELL:
        return "film"
    return ""


#: 下钻指标表（**页面上能点的那些格**）—— kind → 归属行 / 取值字段 / 单位 / 标题 / 折算。
#:
#: ⚠ 金额类跟台数类**常常是同一批行换个取值字段**（贴膜台数 vs 贴膜毛利），
#:   所以判据还是那一份 `row_kind()`，别为金额类另写一遍行判定。
#: ⚠ `factor` 只有新机有（渠道折算 ×0.9），元类一律 1。
DRILL_KINDS: Dict[str, tuple] = {
    "new":          (frozenset(("new_retail", "new_meituan")), "qty", "台",
                     "新机销售", metric.NEW_FACTOR),
    "film":         (frozenset(("film",)), "qty", "台", "达成（贴膜）", 1.0),
    "gift":         (frozenset(("gift",)), "qty", "台", "礼包达成", 1.0),
    "film_profit":  (frozenset(("film",)), "profit", "元", "零售毛利达成", 1.0),
    "gift_profit":  (frozenset(("gift",)), "profit", "元", "礼包毛利达成", 1.0),
    "total_profit": (frozenset(("film", "gift")), "profit", "元", "总毛利达成", 1.0),
}


def _blank_acc() -> Dict[str, float]:
    return {"new_retail": 0.0, "new_meituan": 0.0,
            "film_qty": 0.0, "film_profit": 0.0,
            "gift_qty": 0.0, "gift_profit": 0.0}


def _has_film(rows) -> bool:
    for r in rows:
        if r.get("c2") == "贴膜":
            return True
    return False


def _has_people(d) -> bool:
    rows = (d or {}).get("rows") or []
    if not rows:
        return True
    # 有一家店带 people 键即可（全空数组也算算过了）
    return any("people" in r for r in rows if isinstance(r, dict))


def _people_rows(store: str, pacc: Dict[Tuple[str, str], Dict[str, float]]) -> List[dict]:
    """一家店的人行 —— **同一 `metric.row` 公式**（新机台量 → 目标 / 毛利目标）。

    ⚠ 只留**有数**的人（全 0 不占一行）；没写店员显示「（没写店员）」。
    ⚠ 按个人新机台量降序（主力在前），并列按名字 —— 顺序稳定。
    """
    out: List[dict] = []
    for (st, who), a in pacc.items():
        if st != store:
            continue
        if not any(a.values()):
            continue
        r = metric.row(store, a["new_retail"], a["new_meituan"],
                       a["film_qty"], a["film_profit"],
                       a["gift_qty"], a["gift_profit"],
                       name=who or "（没写店员）")
        out.append(r)
    out.sort(key=lambda r: (-float(r.get("new") or 0), str(r.get("name") or "")))
    return out


def compute(root=None, stores: Optional[List[str]] = None, day=None) -> dict:
    root = Path(root or ROOT)
    start, end = month_window(day)
    names = list(stores) if stores is not None else list(stores_in_scope(root))
    acc: Dict[str, Dict[str, float]] = {n: _blank_acc() for n in names}
    #: 点门店拆到人：`(店, 店员)` → 同一套累加器。没写店员 → `""`（显示时兜底）。
    pacc: Dict[Tuple[str, str], Dict[str, float]] = {}

    src = "erp_sales"
    db = find_db(root)
    warn = ""
    if not (db and db.exists()):
        warn = "找不到订单库（out/cbg-*.db）—— 先跑「抓数据」"
        stream = []
        src = "missing"
    else:
        stream = list(_load_rows_sqlite(db, start, end, stores=names))
        if not _has_film(stream) and stream:
            warn = "本月库里还没有贴膜行（可能刚修过无串号落库，等下一次 erp-dump）"

    as_of = ""
    #: 备注点名别家门店的行 —— **2026-09-29 拍板：算转出店**，不再剔。
    #: 只记台账、在 note 里报出来（不漏不重要看得见，别静默改数）。
    transfer = 0
    fidx = foreign_mod.name_index(root) if src == "erp_sales" else {}
    for r in stream:
        if r.get("ts") and r["ts"] > as_of:
            as_of = r["ts"]
        store = r["店"]
        a = acc.get(store)
        if a is None:
            continue
        # ⚠ 备注点名**别家门店**（转单/转线上）→ **算转出店**（行上的门店）。
        #   用户 2026-09-29 覆盖 2026-09-26 的「整行剔」：转出店没有那个线上平台、
        #   是通过别的店走的量，增值业绩归原门店（与权益页同口径）。
        #   判据仍走 foreign（识别 + 台账），只是不再 continue。
        if foreign_mod.other_store_in(store, r.get("xnote"), fidx):
            transfer += 1
        pa = pacc.setdefault((store, r.get("谁") or ""), _blank_acc())
        qty, profit, typ = r["数量"], r["毛利"], r["类型"]
        c1, c2, name, note = r["c1"], r["c2"], r["名称"], r["note"]

        # ⚠ 归类走 `row_kind()` —— 明细下钻（`drill_rows`）问的是同一个函数，
        #   页面数字和明细合计才不会走散。
        k = row_kind(c1, c2, typ, name, note)
        if k == "new_retail":
            a["new_retail"] += qty
            pa["new_retail"] += qty
        elif k == "new_meituan":
            a["new_meituan"] += qty
            pa["new_meituan"] += qty
        elif k == "gift":
            a["gift_qty"] += qty
            a["gift_profit"] += profit
            pa["gift_qty"] += qty
            pa["gift_profit"] += profit
        elif k == "film":
            a["film_qty"] += qty
            a["film_profit"] += profit
            pa["film_qty"] += qty
            pa["film_profit"] += profit

    regions = stores_by_region(root)
    rows = []
    for n in names:
        r = metric.row(n, acc[n]["new_retail"], acc[n]["new_meituan"],
                       acc[n]["film_qty"], acc[n]["film_profit"],
                       acc[n]["gift_qty"], acc[n]["gift_profit"])
        r["region"] = regions.get(n, "")
        r["people"] = _people_rows(n, pacc)
        rows.append(r)
    note = ("新机=(零售净+美团分销净)×0.9；京东分销不算。台均基线内置 25/30/35/40。"
            "源：仅 erp_sales（SQLite）。")
    if transfer:
        note += " · 备注点名别家门店 %d 行（已计入本店=转出店）" % transfer
    if warn:
        note += " · " + warn
    return {
        "ok": src != "missing",
        "why": warn if src == "missing" else "",
        "start": start,
        "end": end,
        "progress": time_progress(day),
        "data_as_of": as_of[:16] if as_of else "",
        "rows": rows,
        "summary": metric.summarize(rows),
        "source": src,
        "transfer_rows": transfer,
        "note": note,
    }


def drill_rows(root=None, store: str = "", kind: str = "new", day=None) -> dict:
    """某店本窗口**某个指标**纳入统计的销售行 —— 页面那一格数字的明细底稿。

    `kind` 见 `DRILL_KINDS`（页面上能点的那些格：新机 / 贴膜达成 / 礼包达成 /
    三份毛利）。列（用户 2026-09-29 点名，同日追加「带上毛利吧」）：
    **销售单号 · 单据类型 · 商品名称 · 数量 · 销售时间 · 毛利**。
    **含退货**：源数据里退货数量本来就是负数，进明细也进合计（直接相加）。

    ⚠ 行判定 = `row_kind()`（与 `compute` 同一个）⇒ `合计 × factor` 必须
      等于页面那一格的值。改口径只改 `row_kind` / `DRILL_KINDS`，别在这里另写一套。
    ⚠ 老库可能没有 `单号` 列 —— `PRAGMA` 问一下，缺就补 `'' AS 单号`
      （同 `_select_sql` 的兜底，别让整条接口 `no such column` 挂掉）。
    """
    spec = DRILL_KINDS.get(kind)
    if spec is None:
        return {"ok": False, "why": "不认识的指标：%s" % kind}
    kinds, field, unit, label, factor = spec
    root = Path(root or ROOT)
    start, end = month_window(day)
    store = str(store or "").strip()
    if not store:
        return {"ok": False, "why": "没指定门店"}
    db = find_db(root)
    if not (db and db.exists()):
        return {"ok": False, "why": "找不到订单库（out/cbg-*.db）—— 先跑「抓数据」"}
    end_ex = (datetime.date.fromisoformat(end)
              + datetime.timedelta(days=1)).isoformat()
    conn = sqlite3.connect("file:%s?mode=ro" % db, uri=True)
    rows: List[dict] = []
    try:
        conn.row_factory = sqlite3.Row
        try:
            have = {str(r[1]) for r in conn.execute("PRAGMA table_info(erp_sales)")}
        except sqlite3.Error:
            have = set()
        no = '"单号"' if "单号" in have else "'' AS \"单号\""
        cust = '"客户/顾客"' if "客户/顾客" in have else "'' AS \"客户/顾客\""
        sql = ("SELECT " + no + ", 单据类型, 一级分类, 二级分类, 商品名称, 数量,"
               " 零售考核毛利, 支付时间, 备注, 单行备注, " + cust + ", 付款方式"
               " FROM erp_sales"
               " WHERE 支付时间 >= ? AND 支付时间 < ? AND 门店 = ?")
        for r in conn.execute(sql, (start, end_ex, store)):
            # ⚠ 拼法必须跟 `_load_rows_sqlite` 一致 —— 分销判「美团」连客户字段一起看
            note = " ".join(str(r[k] or "") for k in ("备注", "单行备注",
                                                      "客户/顾客", "付款方式"))
            k = row_kind(r["一级分类"] or "", r["二级分类"] or "",
                         r["单据类型"] or "", r["商品名称"] or "", note)
            if k not in kinds:
                continue
            rows.append({
                "no": r["单号"] or "",
                "typ": r["单据类型"] or "",
                "name": r["商品名称"] or "",
                "qty": _f(r["数量"]),
                "profit": _f(r["零售考核毛利"]),
                "ts": r["支付时间"] or "",
            })
    finally:
        conn.close()
    rows.sort(key=lambda x: (str(x["ts"]), str(x["no"])), reverse=True)
    total = sum((x["qty"] if field == "qty" else x["profit"]) for x in rows)
    shown = round(total * factor, 2)
    if field == "qty":
        note = "合计是原始%s数（退货是负数、已在合计里冲减）" % unit
        if factor != 1:
            note += ("；页面上的「%s」= 合计 ×%s（渠道折算）"
                     % (label, ("%g" % factor)))
        else:
            note += "；就是页面上的「%s」" % label
    else:
        note = "合计是这些单的零售考核毛利之和，就是页面上的「%s」" % label
    return {
        "ok": True, "why": "",
        "store": store, "start": start, "end": end,
        "kind": kind, "label": label, "unit": unit, "field": field,
        "rows": rows, "total": total, "factor": factor, "shown": shown,
        "note": note,
    }


#: 进程内缓存：**只看 sqlite 指纹**（不再扫 xlsx）。
_CACHE = {}


def _fp(root: Path, start: str, end: str) -> tuple:
    db = find_db(root)
    if db and db.exists():
        try:
            st = db.stat()
            return (("db", str(db), int(st.st_mtime_ns), int(st.st_size)),)
        except OSError:
            pass
    return (("db", "missing"), _stores_fp(root))
def _stores_fp(root: Path) -> tuple:
    """门店名单指纹 —— 转单识别/台账（foreign.name_index）读它，改名单必须重算
    （只影响 note 里那句台账，**不影响台量归属**）。"""
    p = Path(root) / "config" / "stores.yaml"
    try:
        st = p.stat()
        return ("stores", int(st.st_mtime_ns), int(st.st_size))
    except OSError:
        return ("stores", 0, 0)




def load(root=None, stores=None, day=None, force: bool = False) -> dict:
    """算（或取缓存）。**库**变了才重算。"""
    try:
        root = Path(root or ROOT)
        start, end = month_window(day)
        key = (start, end, tuple(stores) if stores is not None else None, str(root))
        fp = _fp(root, start, end)
        if not force:
            hit = _CACHE.get(key)
            # ⚠ 指纹只看库 —— 改完代码后**同库旧缓存**可能还没有 `people`，
            #   前端 `ppl.length===0` ⇒ 门店格没有 data-film-store ⇒「点了没反应」。
            #   缺 people 就当 miss，逼着重算一次。
            if hit and hit[0] == fp and _has_people(hit[1]):
                return hit[1]
        d = compute(root=root, stores=stores, day=day)
        _CACHE[key] = (fp, d)
        while len(_CACHE) > 8:
            _CACHE.pop(next(iter(_CACHE)))
        return d
    except Exception as e:
        return {"ok": False, "why": "算防护膜达成失败：%s" % e}


def run(root=None, emit=None) -> dict:
    """步骤 `film`：算一遍并落快照 `out/film.json`（留痕，页面仍现算）。"""
    import json
    root = Path(root or ROOT)
    say = emit or (lambda *_a, **_k: None)
    d = load(root=root, force=True)
    if not d.get("ok"):
        say("[防护膜达成] 没算成：%s" % d.get("why"))
        return d
    out = root / "out" / "film.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(d, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(out)
    s = d.get("summary") or {}
    say("[防护膜达成] 已落快照 %s（新机 %s · 达成 %s · 源 %s）"
        % (out, s.get("new"), s.get("done"), d.get("source")))
    d["file"] = str(out)
    return d
