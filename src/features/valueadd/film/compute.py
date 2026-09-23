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
    """时间进度 —— 跟源表 `T1 =(DAY(TODAY())-1)/30` 同一口径。"""
    d = day or datetime.date.today()
    return max(0.0, min(1.0, (d.day - 1) / 30.0))


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
                "ts": r["支付时间"] or "",
            }
    finally:
        conn.close()


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
    for r in stream:
        if r.get("ts") and r["ts"] > as_of:
            as_of = r["ts"]
        store = r["店"]
        a = acc.get(store)
        if a is None:
            continue
        pa = pacc.setdefault((store, r.get("谁") or ""), _blank_acc())
        qty, profit, typ = r["数量"], r["毛利"], r["类型"]
        c1, c2, name, note = r["c1"], r["c2"], r["名称"], r["note"]

        if c1 == "手机":
            if typ in ("零售", "零售退"):
                a["new_retail"] += qty
                pa["new_retail"] += qty
            elif typ in ("分销", "分销退") and "美团" in note:
                a["new_meituan"] += qty
                pa["new_meituan"] += qty
            continue

        if metric.is_gift(name):
            if typ in metric.SELL:
                a["gift_qty"] += qty
                a["gift_profit"] += profit
                pa["gift_qty"] += qty
                pa["gift_profit"] += profit
            continue

        if c2 == "贴膜" and name != metric.SOFT and typ in metric.SELL:
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
    return (("db", "missing"),)


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
