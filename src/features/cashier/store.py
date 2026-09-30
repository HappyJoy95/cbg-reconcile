# -*- coding: utf-8 -*-
"""收银流水 + 政策快照的存储层（收银界面 v1，2026-09-29）。

开发目标见 `.dsh/docs/2026-09-29-生活馆收银界面-开发目标.md`。三件事：

1. **流水 `sale_entries`** —— 人工录的销售事实（时间/编码/名称/数量/金额/销售员/备注），
   可改可删。跟 `profit_result`（利润，派生、重算覆盖）**分两张表**。
2. **政策 `price_policy`** —— pmall「价格与返利政策」导出的整表快照，
   按 `商品编码` 反查商品名/成本/返利（收银编码一填就带出来）。
   ⚠ 政策 9 列**原样**落（表头含 `基准提货价*` 的星号）——
   走 `dump.put` 的动态列，接口改名不用动迁移。
3. **开库即迁移** —— 纯手动收银机可能永远不跑抓取，
   `ensure()` 自己把 `out/cbg-<年>.db` 建出来并跑到最新编号。

⚠ 业务校验回 `{ok: False, why}`（**不抛**）；编程错误才抛。
⚠ 测试必须传 `root=` 临时目录（conftest 连"改已有文件"都会报）。
"""

from __future__ import annotations

import datetime
import json
import math
import time
from pathlib import Path
from typing import List, Optional

# ⚠ 本文件在 src/features/cashier/ ⇒ 到 src 是**三个点**
#   （comparison/store.py 在四层深所以是四个点 —— 别照抄）
from ... import dump as _dump
from ...paths import ROOT
from ...storage import db as _db
from ...storage import migrate as _migrate
from ...storage import schema as _schema
from . import import_cfg

CST = datetime.timezone(datetime.timedelta(hours=8))

#: 流水来源（手输 = manual；以后玲珑导入 = linglong，见开发目标"本版不做的"）
SOURCES = ("manual", "linglong")


# ------------------------------------------------------------------ 库
def db_path(root=None) -> Path:
    """这台机器的库：已有就用最新那份，没有就按**北京时间年份**造 `out/cbg-<年>.db`。"""
    from ...storage import runlog
    existing = runlog.find_db(root)
    if existing:
        return existing
    return _dump.year_db(Path(root or ROOT) / "out", _dump.ts_year(time.time()))


def ensure(root=None) -> Path:
    """确保库和表都在（**开库即迁移**，幂等）。返回库路径；迁移失败原样抛
    （`MigrationError` 带编号和原因，上层转成给界面看的 why）。"""
    path = db_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = _db.connect(str(path))
    try:
        _migrate.run(conn)
    finally:
        conn.close()
    return path


# ---------------------------------------------------------------- 流水
def _clean_items(val, what: str, name_key: str):
    """配件/支付明细 `[{名, 金额}]` → `(list, None)`；坏的回 `(None, why)`。

    金额两位小数、不许负；名字不许空。**不校验支付加总**（用户定：软提醒不拦截）。
    """
    if val in (None, ""):
        return [], None
    if isinstance(val, str):
        try:
            val = json.loads(val)
        except ValueError:
            return None, "%s不是合法 JSON，得是列表" % what
    if not isinstance(val, list):
        return None, "%s得是列表" % what
    out = []
    for i, item in enumerate(val):
        if not isinstance(item, dict):
            return None, "%s第 %d 条不是对象" % (what, i + 1)
        name = str(item.get(name_key) or "").strip()
        if not name:
            return None, "%s第 %d 条没有名字" % (what, i + 1)
        try:
            amt = round(float(item.get("amount")), 2)
        except (TypeError, ValueError):
            return None, "%s「%s」的金额得是数字" % (what, name)
        if not math.isfinite(amt):
            return None, "%s「%s」的金额得是数字" % (what, name)
        if amt < 0:
            return None, "%s「%s」的金额不能是负数" % (what, name)
        out.append({name_key: name, "amount": amt})
    return out, None


def _clean_entry(data: dict, entry_id=None):
    """校验 + 归一 —— 返回 `(行, None)` 或 `(None, why)`。"""
    data = data or {}
    sold_at = str(data.get("sold_at") or "").strip().replace("T", " ")
    if len(sold_at) < 10 or sold_at[4] != "-" or sold_at[7] != "-":
        return None, "销售时间格式不对（要 2026-09-29 这种）"
    try:
        amount = round(float(data.get("amount")), 2)
    except (TypeError, ValueError):
        return None, "金额得是数字"
    if not math.isfinite(amount):
        return None, "金额得是数字"
    if amount < 0:
        return None, "金额不能是负数"
    qty_raw = data.get("quantity")
    if qty_raw in (None, ""):
        qty_raw = 1
    try:
        qty = float(qty_raw)
    except (TypeError, ValueError):
        return None, "数量得是数字"
    if not math.isfinite(qty) or qty <= 0:
        return None, "数量得大于 0"
    source = str(data.get("source") or "manual").strip() or "manual"
    if source not in SOURCES:
        return None, "来源只认 %s" % "/".join(SOURCES)
    sn = str(data.get("sn") or "").strip()
    acc, why = _clean_items(data.get("accessories"), "配件", "name")
    if why:
        return None, why
    pay, why = _clean_items(data.get("payments"), "支付", "method")
    if why:
        return None, why
    now = datetime.datetime.now(CST).strftime("%Y-%m-%d %H:%M:%S")
    return {
        "sold_at": sold_at,
        "goods_code": str(data.get("goods_code") or "").strip(),
        "goods_name": str(data.get("goods_name") or "").strip(),
        "quantity": qty,
        "amount": amount,
        "seller": str(data.get("seller") or "").strip(),
        "note": str(data.get("note") or "").strip(),
        "source": source,
        "sn": sn,
        "accessories": json.dumps(acc, ensure_ascii=False),
        "payments": json.dumps(pay, ensure_ascii=False),
    }, None


def save_entry(root=None, data: dict = None, entry_id=None) -> dict:
    """录/改一笔。给了 `entry_id` 就改那条（不存在回 why，不静默新建）。"""
    row, why = _clean_entry(data, entry_id)
    if why:
        return {"ok": False, "why": why}
    now = datetime.datetime.now(CST).strftime("%Y-%m-%d %H:%M:%S")
    path = ensure(root)
    with _db.tx(str(path)) as conn:
        if entry_id:
            try:
                cur = conn.execute(
                    "UPDATE sale_entries SET sold_at=?, goods_code=?, goods_name=?,"
                    " quantity=?, amount=?, seller=?, note=?, source=?, sn=?,"
                    " accessories=?, payments=?, updated_at=?"
                    " WHERE id=?",
                    (row["sold_at"], row["goods_code"], row["goods_name"],
                     row["quantity"], row["amount"], row["seller"], row["note"],
                     row["source"], row["sn"], row["accessories"],
                     row["payments"], now, int(entry_id)))
            except (TypeError, ValueError):
                return {"ok": False, "why": "id 不对"}
            if cur.rowcount == 0:
                return {"ok": False, "why": "没有这条流水（id=%s）" % entry_id}
            return {"ok": True, "id": int(entry_id)}
        cur = conn.execute(
            "INSERT INTO sale_entries (sold_at, goods_code, goods_name, quantity,"
            " amount, seller, note, source, sn, accessories, payments,"
            " created_at, updated_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (row["sold_at"], row["goods_code"], row["goods_name"], row["quantity"],
             row["amount"], row["seller"], row["note"], row["source"], row["sn"],
             row["accessories"], row["payments"], now, now))
        return {"ok": True, "id": int(cur.lastrowid)}


def delete_entry(root=None, entry_id=None) -> dict:
    """删一笔。"""
    try:
        eid = int(entry_id)
    except (TypeError, ValueError):
        return {"ok": False, "why": "id 不对"}
    with _db.tx(str(ensure(root))) as conn:
        cur = conn.execute("DELETE FROM sale_entries WHERE id=?", (eid,))
        if cur.rowcount == 0:
            return {"ok": False, "why": "没有这条流水（id=%s）" % eid}
        return {"ok": True, "id": eid}


def exclude_entry(root=None, entry_id=None) -> dict:
    """玲珑卡的 ✕ —— **软排除**（记标记，不删 dump 库的原单，再导入也不回来）。

    手工单（source=manual）不许走这儿：它的 ✕ 是真删（`delete_entry`）。
    """
    try:
        eid = int(entry_id)
    except (TypeError, ValueError):
        return {"ok": False, "why": "id 不对"}
    with _db.tx(str(ensure(root))) as conn:
        row = conn.execute("SELECT source FROM sale_entries WHERE id=?",
                           (eid,)).fetchone()
        if not row:
            return {"ok": False, "why": "没有这条流水（id=%s）" % eid}
        if (row[0] or "manual") != "linglong":
            return {"ok": False, "why": "手工流水请用删除，不是排除"}
        conn.execute("UPDATE sale_entries SET excluded=1, updated_at=? WHERE id=?",
                     (datetime.datetime.now(CST).strftime("%Y-%m-%d %H:%M:%S"), eid))
        return {"ok": True, "id": eid}


def _row_out(r) -> dict:
    """行 → 给界面的形状：JSON 列拆开、NULL 兜底（老行没有新列的值）。"""
    d = dict(r)
    d["sn"] = d.get("sn") or ""
    d["external_id"] = d.get("external_id") or ""
    d["excluded"] = int(d.get("excluded") or 0)
    for k in ("accessories", "payments"):
        try:
            v = json.loads(d.get(k) or "[]")
        except ValueError:
            v = []
        d[k] = v if isinstance(v, list) else []
    return d


def list_entries(root=None, day: str = "") -> List[dict]:
    """流水（`day` = `YYYY-MM-DD` 过滤那天；空 = 全部）。**新→旧**，排除软排除的行。"""
    path = ensure(root)
    conn = _db.open_db(str(path))
    try:
        if day:
            rows = conn.execute(
                "SELECT * FROM sale_entries"
                " WHERE (excluded IS NULL OR excluded=0) AND sold_at LIKE ?"
                " ORDER BY sold_at DESC, id DESC", (str(day).strip() + "%",)).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM sale_entries"
                " WHERE excluded IS NULL OR excluded=0"
                " ORDER BY sold_at DESC, id DESC").fetchall()
        return [_row_out(r) for r in rows]
    finally:
        conn.close()


def sellers(root=None) -> List[str]:
    """出现过的销售员（**最近出现的排前面**）—— 收银下拉的唯一"名单"来源。"""
    conn = _db.open_db(str(ensure(root)))
    try:
        rows = conn.execute(
            "SELECT seller, MAX(id) AS m FROM sale_entries"
            " WHERE seller IS NOT NULL AND seller != ''"
            " GROUP BY seller ORDER BY m DESC").fetchall()
        return [r["seller"] for r in rows]
    finally:
        conn.close()


def entries_from_orders(root=None, day: str = "") -> dict:
    """把 `orders` 表里那天的销售单变成当日卡片（`source='linglong'`）。

    ⚠ **只读本机库**：拉网络在 web.App.cashier_import 那层做（先合并 orders，
      再调这里）—— 这样本函数纯读、零网络，好测。
    ⚠ 过滤两道：① 备注命中黑名单（`import_cfg.load`）跳过；
      ② `external_id` 已存在（**含软排除的** —— excluded 行不删）跳过 ⇒ 幂等。
    ⚠ **单据一卡**：多行聚合（数量=Σ、名称=首行+等N件、编码/SN 拼接）。
    """
    day = str(day or "").strip()
    if len(day) != 10 or day[4] != "-" or day[7] != "-":
        return {"ok": False, "why": "日期格式不对（要 2026-09-30）"}
    path = ensure(root)
    blacklist = import_cfg.load(root)
    now = datetime.datetime.now(CST).strftime("%Y-%m-%d %H:%M:%S")
    imported = skipped_black = skipped_dup = 0
    conn = _db.open_db(str(path))
    try:
        try:
            orders = conn.execute(
                "SELECT document_no, doc_create_time, included_tax_amount, remark,"
                " consumer_guide_name FROM orders WHERE doc_create_time LIKE ?"
                " ORDER BY doc_create_time",
                (day + "%",)).fetchall()
        except Exception:                                   # noqa: BLE001
            return {"ok": False, "why": "还没有 orders 表（先点一次导入拉单）"}
        have = {r[0] for r in conn.execute(
            "SELECT external_id FROM sale_entries"
            " WHERE external_id IS NOT NULL AND external_id != ''").fetchall()}
        for o in orders:
            dn = o["document_no"]
            remark = str(o["remark"] or "")
            if any(w and w in remark for w in blacklist):
                skipped_black += 1
                continue
            if dn in have:
                skipped_dup += 1
                continue
            lines = conn.execute(
                "SELECT sn, ean, item_name, quantity FROM order_lines"
                " WHERE document_no=? ORDER BY line_no", (dn,)).fetchall()
            qty = sum(float(ln["quantity"] or 0) for ln in lines) or 1
            names = [str(ln["item_name"] or "") for ln in lines if ln["item_name"]]
            name = (names[0] if names else "") + (
                " 等%d件" % len(lines) if len(lines) > 1 else "")
            codes = [str(ln["ean"] or "") for ln in lines if ln["ean"]]
            sns = [str(ln["sn"] or "") for ln in lines if ln["sn"]]
            amount = float(o["included_tax_amount"] or 0)
            if not math.isfinite(amount):
                amount = 0.0            # 防 nan/inf 挂整页读（同 _clean_entry 口径）
            conn.execute(
                "INSERT INTO sale_entries (sold_at, goods_code, goods_name,"
                " quantity, amount, seller, note, source, sn, external_id,"
                " created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (o["doc_create_time"], "|".join(codes), name.strip(),
                 qty, amount,
                 str(o["consumer_guide_name"] or ""), remark,
                 "linglong", "|".join(sns), dn, now, now))
            have.add(dn)
            imported += 1
        conn.commit()
    finally:
        conn.close()
    return {"ok": True, "imported": imported,
            "skipped_blacklist": skipped_black, "skipped_dup": skipped_dup}


# ---------------------------------------------------------------- 政策
def save_policy(root=None, rows: list = None, fetched_at: str = "") -> dict:
    """政策整表快照（**先清后写** —— 政策是"当前版"，不留历史）。

    `rows` = `pmall.parse_policy` 的 dict 列表（表头原样，金额是文本不转）。
    """
    rows = rows or []
    if not rows:
        return {"ok": False, "why": "没有行可存"}
    fetched = (fetched_at or datetime.datetime.now(CST)
               .strftime("%Y-%m-%d %H:%M:%S"))
    written = 0
    with _db.tx(str(ensure(root))) as conn:
        conn.execute("DELETE FROM price_policy")
        for r in rows:
            if not isinstance(r, dict):
                continue
            row = dict(r)
            code = str(row.get("goods_code") or row.get("商品编码") or "").strip()
            if not code:
                continue                       # 没编码的行反查不到，不进库
            row["goods_code"] = code
            row["fetched_at"] = fetched
            _dump.put(conn, "price_policy", row)   # 动态列（含星号表头）
            written += 1
    return {"ok": True, "rows": written, "fetched_at": fetched}


def lookup(root=None, code: str = "") -> Optional[dict]:
    """按商品编码反查政策（最新一份快照）。查不到回 `None`。"""
    code = str(code or "").strip()
    if not code:
        return None
    conn = _db.open_db(str(ensure(root)))
    try:
        row = conn.execute(
            "SELECT * FROM price_policy WHERE goods_code=?"
            " ORDER BY fetched_at DESC LIMIT 1", (code,)).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def policy_meta(root=None) -> dict:
    """政策表新鲜度（页面显示"上次刷新 + 多少行"）。没表/空表也回得体面值。"""
    try:
        conn = _db.open_db(str(ensure(root)))
    except Exception:                                     # noqa: BLE001
        return {"rows": 0, "fetched_at": ""}
    try:
        row = conn.execute(
            "SELECT COUNT(*) AS n, MAX(fetched_at) AS f FROM price_policy").fetchone()
        return {"rows": int(row["n"] or 0), "fetched_at": row["f"] or ""}
    except Exception:                                     # noqa: BLE001
        return {"rows": 0, "fetched_at": ""}
    finally:
        conn.close()


__all__ = [
    "SOURCES", "db_path", "ensure",
    "save_entry", "delete_entry", "list_entries", "sellers",
    "exclude_entry", "entries_from_orders",
    "save_policy", "lookup", "policy_meta",
]
