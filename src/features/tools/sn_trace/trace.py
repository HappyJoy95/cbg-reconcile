# -*- coding: utf-8 -*-
"""串号全程追踪 —— 本地库上的「86 码 ↔ SN ↔ 库存/销售」关联查询。

**零网络**：只读 `out/cbg-<年>.db`。

关联算法（两轮扩散，够门店用）：
  1. 以查询码为种子，在 `erp_stock`（**全部快照**）四列里命中 → 收下整行串号；
  2. 同批码去 `erp_sales` 的 `sn/串号/串号2/串号3` 命中 → 收下销售行串号；
  3. 再用扩大后的集合回扫一遍库存（销售副列里的 SN 可能对应另一行库存）。

真 SN 判定复用 `claim.pending.metric`（16 位优先、拒绝回落 IMEI）。
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Set, Tuple

from ..claim.pending import metric

#: 库存参与匹配的列（与 erp_stock 实际列名一致）
STOCK_COLS = ("sn", "imei", "sub_imei", "sub_imei1")
#: 销售参与匹配的列
SALES_COLS = ("sn", "串号", "串号2", "串号3")

#: 每边最多回多少条，防止全表刷屏（按时间倒序在 SQL 里排）
LIMIT = 200


def find_db(root) -> Optional[Path]:
    from ....app import data_state
    found = data_state._find_db(Path(root))
    return Path(found) if found else None


def _norm(code: str) -> str:
    return str(code or "").strip()


def _codes_from_values(values: Iterable) -> Set[str]:
    out: Set[str] = set()
    for v in values:
        t = _norm(v)
        if not t or t == "-" or t.startswith("nosn:"):
            continue
        # 有的格子里挤了多个码（空格/逗号）
        for part in str(t).replace("，", " ").replace(",", " ").split():
            p = part.strip()
            if len(p) >= 8:
                out.add(p)
    return out


def _stock_hits(conn: sqlite3.Connection, codes: Set[str]) -> List[dict]:
    """库存快照里串号落在 `codes` 的行（全部快照日）。"""
    if not codes:
        return []
    have = {str(r[1]) for r in conn.execute("PRAGMA table_info(erp_stock)")}
    use = [c for c in STOCK_COLS if c in have]
    if not use:
        return []
    mark = ",".join("?" for _ in codes)
    ors = " OR ".join('"%s" IN (%s)' % (c, mark) for c in use)
    extra = [c for c in ("pro_name", "store_name", "status", "snapshot_date")
             if c in have]
    select = ", ".join('"%s"' % c for c in use + extra)
    sql = ('SELECT %s FROM erp_stock WHERE %s '
           'ORDER BY snapshot_date DESC' % (select, ors))
    params = list(codes) * len(use)
    rows = []
    for r in conn.execute(sql, params):
        d = {c: (r[c] if c in r.keys() else None) for c in use + extra}
        # 整行串号集合
        row_codes = _codes_from_values(d.get(c) for c in use)
        d["_codes"] = sorted(row_codes)
        rows.append(d)
    return rows


def _sales_hits(conn: sqlite3.Connection, codes: Set[str],
                stores: Optional[Set[str]] = None) -> List[dict]:
    """销售明细里任一串号列落在 `codes` 的行。"""
    if not codes:
        return []
    have = {str(r[1]) for r in conn.execute("PRAGMA table_info(erp_sales)")}
    use = [c for c in SALES_COLS if c in have]
    if not use:
        return []
    mark = ",".join("?" for _ in codes)
    ors = " OR ".join('"%s" IN (%s)' % (c, mark) for c in use)
    want_cols = []
    for c in ("门店", "店员", "商品名称", "支付时间", "单号", "单据类型",
              "串号标识", "一级分类", "备注"):
        if c in have:
            want_cols.append(c)
    select = ", ".join('"%s"' % c for c in want_cols + use)
    sql = ('SELECT %s FROM erp_sales WHERE %s '
           'ORDER BY 支付时间 DESC' % (select, ors)
           if "支付时间" in have else
           'SELECT %s FROM erp_sales WHERE %s' % (select, ors))
    params = list(codes) * len(use)
    rows = []
    for r in conn.execute(sql, params):
        d = {c: r[c] for c in want_cols}
        row_codes = _codes_from_values(r[c] for c in use)
        d["_codes"] = sorted(row_codes)
        store = str(d.get("门店") or "")
        if stores is not None and store and store not in stores:
            # 仍把码收进关联集（别家店卖的也可能是同一台）——
            # 但明细列表按身份滤掉。码在调用方合并时用 _codes_all。
            d["_filtered"] = True
        else:
            d["_filtered"] = False
        rows.append(d)
    return rows


def collect(root, code: str, stores: Optional[Set[str]] = None) -> dict:
    """一次追踪。`stores=None` = 销售不滤店（平台）；否则销售明细按店滤。"""
    q = _norm(code)
    if not q:
        return {"ok": False, "why": "请输入 86 码或 SN"}
    db = find_db(root)
    if not db:
        return {"ok": False, "why": "还没有本地销售/库存库 —— 先跑「抓取云商数据」"}

    seed = {q}
    # 也接受「主串 副串」粘贴
    seed |= _codes_from_values([q])

    conn = sqlite3.connect("file:%s?mode=ro" % db, uri=True)
    try:
        conn.row_factory = sqlite3.Row
        # 两轮：种子 → 销售/库存 → 扩码 → 再扫
        codes: Set[str] = set(seed)
        stock_rows: List[dict] = []
        sales_rows: List[dict] = []
        for _round in range(2):
            s_hits = _stock_hits(conn, codes)
            p_hits = _sales_hits(conn, codes, stores=stores)
            stock_rows = s_hits
            sales_rows = p_hits
            grew = False
            for row in s_hits:
                for c in row.get("_codes") or ():
                    if c not in codes:
                        codes.add(c)
                        grew = True
            for row in p_hits:
                for c in row.get("_codes") or ():
                    if c not in codes:
                        codes.add(c)
                        grew = True
            if not grew:
                break
    finally:
        conn.close()

    # 分类
    imeis = sorted(c for c in codes if metric.looks_like_imei(c))
    true_sn = metric.pick_true_sn(*sorted(codes)) or ""
    # 查询本身若是 SN，优先展示它
    if metric.sn_kind(q) == "sn":
        true_sn = q

    # 库存展示：去掉内部键；限制条数（已按日期 DESC）
    stock_out = []
    for row in stock_rows[:LIMIT]:
        stock_out.append({
            "date": row.get("snapshot_date") or "",
            "store": row.get("store_name") or "",
            "name": row.get("pro_name") or "",
            "status": row.get("status") or "",
            "sn": row.get("sn") or "",
            "imei": row.get("imei") or "",
            "sub_imei": row.get("sub_imei") or "",
            "sub_imei1": row.get("sub_imei1") or "",
            "codes": row.get("_codes") or [],
        })

    sales_out = []
    sales_codes: Set[str] = set()
    for row in sales_rows:
        sales_codes |= set(row.get("_codes") or ())
        if row.get("_filtered"):
            continue
        if len(sales_out) >= LIMIT:
            continue
        sales_out.append({
            "store": row.get("门店") or "",
            "who": row.get("店员") or "",
            "name": row.get("商品名称") or "",
            "ts": row.get("支付时间") or "",
            "doc": row.get("单号") or "",
            "typ": row.get("单据类型") or "",
            "marker": row.get("串号标识") or "",
            "codes": row.get("_codes") or [],
        })

    # 被身份滤掉的销售条数（提示「还有别家店的记录」）
    sales_filtered = sum(1 for r in sales_rows if r.get("_filtered"))

    kind = metric.sn_kind(q)
    label = {"imei": "86码（IMEI）", "sn": "设备 SN", "": "未识别码"}.get(kind, kind)

    note_bits = []
    if kind == "imei" and true_sn:
        note_bits.append("已由库存/关联列反查到真 SN")
    elif kind == "imei" and not true_sn:
        note_bits.append("本地库尚未反查到真 SN —— 可看下方库存/销售轨迹")
    if not stock_out and not sales_out:
        note_bits.append("本地库没有命中记录（可能已超快照保留期或未抓取）")
    if sales_filtered:
        note_bits.append("另有 %d 条销售在身份范围外（未展示明细）" % sales_filtered)

    return {
        "ok": True,
        "query": q,
        "query_kind": kind,
        "query_label": label,
        "sn": true_sn,
        "imeis": imeis,
        "related": sorted(codes),
        "stock": stock_out,
        "stock_total": len(stock_rows),
        "sales": sales_out,
        "sales_total": len([r for r in sales_rows if not r.get("_filtered")]),
        "as_of": (stock_out[0]["date"] if stock_out else ""),
        "note": "；".join(note_bits) or "本地库命中",
        "src": "erp_stock+erp_sales",
    }
