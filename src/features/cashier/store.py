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

#: 品类清单（2026-09-30 用户定：**录入时选**）—— 取值来自用户那份
#: 《9月份机场销售表》的实测值，导出时原样落「品类」列。
CATEGORIES = ("手机", "平板", "笔记本", "穿戴", "音频", "配件", "第三方配件", "服务")

#: 暂存态（2026-09-30 两段式）：`staged` = 还没点「保存并记录」，`saved` = 已入账。
STAGED = "staged"
SAVED = "saved"


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
def _clean_items(val, what: str, name_key: str, with_qty: bool = False):
    """配件/支付明细 `[{名, 金额}]` → `(list, None)`；坏的回 `(None, why)`。

    金额两位小数、不许负；名字不许空。**不校验支付加总**（用户定：软提醒不拦截）。
    `with_qty=True`（配件）再收 `quantity` —— 缺省 1、必须 > 0
 （用户：配件也带数量，导出跟商品行对齐）。
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
        row = {name_key: name, "amount": amt}
        if with_qty:
            qty_raw = item.get("quantity")
            if qty_raw in (None, ""):
                qty_raw = 1
            try:
                qty = float(qty_raw)
            except (TypeError, ValueError):
                return None, "%s「%s」的数量得是数字" % (what, name)
            if not math.isfinite(qty) or qty <= 0:
                return None, "%s「%s」的数量得大于 0" % (what, name)
            row["quantity"] = qty
        out.append(row)
    return out, None


def _clean_products(val):
    """商品行 `[{名, 编码, SN, 数量, 金额, 品类}]` → `(list, None)` / `(None, why)`。

    跟配件（`_clean_items`）的区别：多 编码/SN/数量/品类，**缺名字就不收**。
    ⚠ **品类跟着单条商品走**（2026-09-30 用户定）：行上校验，可空但只认
      `CATEGORIES`。金额两位小数、不许负；数量必须 > 0。
    """
    if val in (None, ""):
        return [], None
    if isinstance(val, str):
        try:
            val = json.loads(val)
        except ValueError:
            return None, "商品不是合法 JSON，得是列表"
    if not isinstance(val, list):
        return None, "商品得是列表"
    out = []
    for i, item in enumerate(val):
        if not isinstance(item, dict):
            return None, "商品第 %d 条不是对象" % (i + 1)
        name = str(item.get("name") or "").strip()
        if not name:
            return None, "商品第 %d 条没有名字" % (i + 1)
        category = str(item.get("category") or "").strip()
        if category and category not in CATEGORIES:
            return None, "品类只认：%s" % "、".join(CATEGORIES)
        qty_raw = item.get("quantity")
        if qty_raw in (None, ""):
            qty_raw = 1
        try:
            qty = float(qty_raw)
        except (TypeError, ValueError):
            return None, "商品「%s」的数量得是数字" % name
        if not math.isfinite(qty) or qty <= 0:
            return None, "商品「%s」的数量得大于 0" % name
        try:
            amt = round(float(item.get("amount")), 2)
        except (TypeError, ValueError):
            return None, "商品「%s」的金额得是数字" % name
        if not math.isfinite(amt):
            return None, "商品「%s」的金额得是数字" % name
        if amt < 0:
            return None, "商品「%s」的金额不能是负数" % name
        out.append({
            "name": name,
            "code": str(item.get("code") or "").strip(),
            "sn": str(item.get("sn") or "").strip(),
            "quantity": qty,
            "amount": amt,
            "category": category,
        })
    return out, None


def _clean_entry(data: dict, entry_id=None):
    """校验 + 归一 —— 返回 `(行, None)` 或 `(None, why)`。

    ⚠ **商品行优先**（2026-09-30 第三轮）：`products` 非空时，
      合计 = Σ商品金额、件数 = Σ数量、顶层品类 = 首行品类
 （传进来的 `amount` / `quantity` / `category` 都不作数 —— 界面上那三项
      在有商品行时就是派生值）。`products` 空 = 老口径（顶层字段说了算）。
    """
    data = data or {}
    sold_at = str(data.get("sold_at") or "").strip().replace("T", " ")
    if len(sold_at) < 10 or sold_at[4] != "-" or sold_at[7] != "-":
        return None, "销售时间格式不对（要 2026-09-29 这种）"
    prod, why = _clean_products(data.get("products"))
    if why:
        return None, why
    # 配件先解出来：应收（amount）= 商品 + 配件（用户 2026-09-30 第 11 轮）
    acc, why = _clean_items(data.get("accessories"), "配件", "name", with_qty=True)
    if why:
        return None, why
    # 顶层显示字段（名/编码/SN）在有商品行时**照首行派生** ——
    # 卡内编辑撤掉了那三个输入框（用户第 1 条：左上角冗余），它们只能服务端算
    goods_name = str(data.get("goods_name") or "").strip()
    goods_code = str(data.get("goods_code") or "").strip()
    sn = str(data.get("sn") or "").strip()
    if prod:
        goods_name = prod[0]["name"] + (
            " 等%d件" % len(prod) if len(prod) > 1 else "")
        goods_code = "|".join([p["code"] for p in prod if p["code"]])
        sn = "|".join([p["sn"] for p in prod if p["sn"]])
        # 应收 = Σ(数量 × 金额) —— 商品和配件的「金额」框都是**单价**
        # （用户 2026-09-30：「不是数量*金额之和吗，2台的时候不动啊」）
        # 件数仍只算商品数量
        amount = round(sum(p["quantity"] * p["amount"] for p in prod)
                       + sum((a.get("quantity") or 1) * a["amount"] for a in acc),
                       2)
        qty = float(sum(p["quantity"] for p in prod))
        category = prod[0]["category"]
    else:
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
        category = str(data.get("category") or "").strip()
        if category and category not in CATEGORIES:
            return None, "品类只认：%s" % "、".join(CATEGORIES)
    source = str(data.get("source") or "manual").strip() or "manual"
    if source not in SOURCES:
        return None, "来源只认 %s" % "/".join(SOURCES)
    pay, why = _clean_items(data.get("payments"), "支付", "method")
    if why:
        return None, why
    now = datetime.datetime.now(CST).strftime("%Y-%m-%d %H:%M:%S")
    return {
        "sold_at": sold_at,
        "goods_code": goods_code,
        "goods_name": goods_name,
        "quantity": qty,
        "amount": amount,
        "seller": str(data.get("seller") or "").strip(),
        "note": str(data.get("note") or "").strip(),
        "source": source,
        "sn": sn,
        "category": category,
        # ⚠ 只认字面 `staged`，别的都当已入库 —— 调用方（前端「确认添加」/
        #   导入）想暂存就显式给；改老行时这个键**根本不进 UPDATE**（见下）。
        "status": STAGED if str(data.get("status") or "").strip() == STAGED else SAVED,
        "products": json.dumps(prod, ensure_ascii=False),
        "accessories": json.dumps(acc, ensure_ascii=False),
        "payments": json.dumps(pay, ensure_ascii=False),
    }, None


def save_entry(root=None, data: dict = None, entry_id=None, store_code=None) -> dict:
    """录/改一笔。给了 `entry_id` 就改那条（不存在回 why，不静默新建）。"""
    from . import ownership
    row, why = _clean_entry(data, entry_id)
    if why:
        return {"ok": False, "why": why}
    now = datetime.datetime.now(CST).strftime("%Y-%m-%d %H:%M:%S")
    path = ensure(root)
    with _db.tx(str(path)) as conn:
        conn.execute('BEGIN IMMEDIATE')
        denied = ownership.failure(root, store_code, write=True, conn=conn)
        if denied:
            return denied
        if entry_id:
            try:
                # ⚠ UPDATE **故意不带 status**：改一笔不改变它入没入库
                #   （已入库的编辑完还是已入库，暂存的还是暂存）——
                #   前端编辑体里就算带着 status 也按本行原值走。
                cur = conn.execute(
                    "UPDATE sale_entries SET sold_at=?, goods_code=?, goods_name=?,"
                    " quantity=?, amount=?, seller=?, note=?, source=?, sn=?,"
                    " category=?, products=?, accessories=?, payments=?,"
                    " updated_at=?"
                    " WHERE id=?",
                    (row["sold_at"], row["goods_code"], row["goods_name"],
                     row["quantity"], row["amount"], row["seller"], row["note"],
                     row["source"], row["sn"], row["category"], row["products"],
                     row["accessories"], row["payments"], now, int(entry_id)))
            except (TypeError, ValueError):
                return {"ok": False, "why": "id 不对"}
            if cur.rowcount == 0:
                return {"ok": False, "why": "没有这条流水（id=%s）" % entry_id}
            return {"ok": True, "id": int(entry_id)}
        cur = conn.execute(
            "INSERT INTO sale_entries (sold_at, goods_code, goods_name, quantity,"
            " amount, seller, note, source, sn, category, status, products,"
            " accessories, payments, created_at, updated_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (row["sold_at"], row["goods_code"], row["goods_name"], row["quantity"],
             row["amount"], row["seller"], row["note"], row["source"], row["sn"],
             row["category"], row["status"], row["products"], row["accessories"],
             row["payments"], now, now))
        return {"ok": True, "id": int(cur.lastrowid)}


def delete_entry(root=None, entry_id=None, store_code=None) -> dict:
    """删一笔。"""
    from . import ownership
    try:
        eid = int(entry_id)
    except (TypeError, ValueError):
        return {"ok": False, "why": "id 不对"}
    with _db.tx(str(ensure(root))) as conn:
        conn.execute('BEGIN IMMEDIATE')
        denied = ownership.failure(root, store_code, write=True, conn=conn)
        if denied:
            return denied
        cur = conn.execute("DELETE FROM sale_entries WHERE id=?", (eid,))
        if cur.rowcount == 0:
            return {"ok": False, "why": "没有这条流水（id=%s）" % eid}
        return {"ok": True, "id": eid}


def exclude_entry(root=None, entry_id=None, store_code=None) -> dict:
    """玲珑卡的 ✕ —— **软排除**（记标记，不删 dump 库的原单，再导入也不回来）。

    手工单（source=manual）不许走这儿：它的 ✕ 是真删（`delete_entry`）。
    """
    from . import ownership
    try:
        eid = int(entry_id)
    except (TypeError, ValueError):
        return {"ok": False, "why": "id 不对"}
    with _db.tx(str(ensure(root))) as conn:
        conn.execute('BEGIN IMMEDIATE')
        denied = ownership.failure(root, store_code, write=True, conn=conn)
        if denied:
            return denied
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
    d["category"] = d.get("category") or ""
    # ⚠ 老行 status 是 NULL（m008 之前入的账）⇒ 当已入库，绝不能当暂存
    d["status"] = STAGED if (d.get("status") or "") == STAGED else SAVED
    for k in ("products", "accessories", "payments"):
        try:
            v = json.loads(d.get(k) or "[]")
        except ValueError:
            v = []
        d[k] = v if isinstance(v, list) else []
    return d


def list_entries(root=None, day: str = "") -> List[dict]:
    """流水（`day` = `YYYY-MM-DD` 过滤那天；空 = 全部）。

    **排序 = 最近触达在最上面**（用户 2026-09-30：最晚加入的在最上面，
    改过的也在最上面，位置别乱动）—— `updated_at DESC, id DESC`。
    ⚠ 跟销售时间**脱钩**：改销售时间不会让卡片在列表里乱跑。
    排除软排除的行。
    """
    path = ensure(root)
    conn = _db.open_db(str(path))
    try:
        if day:
            rows = conn.execute(
                "SELECT * FROM sale_entries"
                " WHERE (excluded IS NULL OR excluded=0) AND sold_at LIKE ?"
                " ORDER BY updated_at DESC, id DESC", (str(day).strip() + "%",)).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM sale_entries"
                " WHERE excluded IS NULL OR excluded=0"
                " ORDER BY updated_at DESC, id DESC").fetchall()
        return [_row_out(r) for r in rows]
    finally:
        conn.close()


def commit_entries(root=None, day: str = "", store_code=None) -> dict:
    """「保存并记录」—— 把**那天**的暂存行转成已入库（`status` staged → saved）。

    ⚠ 只转点名那天：暂存是"今天这一屏还没结的账"，别的天的暂存不该被顺手结掉。
    ⚠ 没有暂存也回 `ok`（重复点 / 空手点不该报错），`saved` 说清转了几条。
    """
    from . import ownership
    day = str(day or "").strip()
    if len(day) != 10 or day[4] != "-" or day[7] != "-":
        return {"ok": False, "why": "日期格式不对（要 2026-09-30）"}
    with _db.tx(str(ensure(root))) as conn:
        conn.execute('BEGIN IMMEDIATE')
        denied = ownership.failure(root, store_code, write=True, conn=conn)
        if denied:
            return denied
        # ⚠ 只改 status，**不碰 updated_at** —— 「保存并记录」是入账动作，
        #   不是"改了内容"，动了它整屏卡片会跟着重排（用户：别乱动位置）
        cur = conn.execute(
            "UPDATE sale_entries SET status=?"
            " WHERE status=? AND sold_at LIKE ?",
            (SAVED, STAGED, day + "%"))
        return {"ok": True, "saved": int(cur.rowcount or 0), "day": day}


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


def entries_from_orders(root=None, day: str = "", store_code=None) -> dict:
    """把 `orders` 表里那天的销售单变成当日卡片（`source='linglong'`）。

    ⚠ **只读本机库**：拉网络在 web.App.cashier_import 那层做（先合并 orders，
      再调这里）—— 这样本函数纯读、零网络，好测。
    ⚠ 过滤两道：① 备注命中黑名单（`import_cfg.load`）跳过；
      ② `external_id` 已存在（**含软排除的** —— excluded 行不删）跳过 ⇒ 幂等。
      ⚠ 只匹配 orders.remark 一列 —— 状态/标签不在过滤范围（spec 口径就是「备注」）。
    ⚠ **单据一卡**：多行聚合（数量=Σ、名称=首行+等N件、编码/SN 拼接），
      **同时把每个订单行写进 `products` 商品行**（2026-09-30 第三轮：
      导出跟卡片商品行走，人改了卡导出就跟着变）。
    ⚠ 生成的卡片是**暂存**（`status='staged'`）—— 2026-09-30 两段式：
      导入不入账，等汇总条「保存并记录」那天一起转正。
    """
    from . import ownership
    day = str(day or "").strip()
    if len(day) != 10 or day[4] != "-" or day[7] != "-":
        return {"ok": False, "why": "日期格式不对（要 2026-09-30）"}
    path = ensure(root)
    blacklist = import_cfg.load(root)
    now = datetime.datetime.now(CST).strftime("%Y-%m-%d %H:%M:%S")
    imported = skipped_black = skipped_dup = 0
    with _db.tx(str(path), named=True) as conn:
        conn.execute('BEGIN IMMEDIATE')
        denied = ownership.failure(root, store_code, write=True, conn=conn)
        if denied:
            return denied
        try:
            from ...modules.auth import runtime
            bound = conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='cashier_owner'").fetchone()
            scoped = bool(store_code) and (runtime.is_lifehall(root) or bool(bound))
            extra = " AND store_code = ?" if scoped else ""
            params = (day + "%", store_code) if scoped else (day + "%",)
            orders = conn.execute(
                "SELECT document_no, doc_create_time, included_tax_amount, remark,"
                " consumer_guide_name FROM orders WHERE doc_create_time LIKE ?"
                 + extra + " ORDER BY doc_create_time",
                params).fetchall()
        except Exception:                                   # noqa: BLE001
            return {"ok": False, "why": "还没有 orders 表（先点一次导入拉单）"}
        have = {r[0] for r in conn.execute(
            "SELECT external_id FROM sale_entries"
            " WHERE external_id IS NOT NULL AND external_id != ''").fetchall()}
        for o in orders:
            dn = str(o["document_no"] or "").strip()
            remark = str(o["remark"] or "")
            if any(w and w in remark for w in blacklist):
                skipped_black += 1
                continue
            # 空单号也按"重导跳过"算：have 里有 '' 会挡住下一张空单号的卡，
            # 宁可每导一次只留一张，也别导一次多一张
            if not dn or dn in have:
                skipped_dup += 1
                continue
            # ⚠ SELECT * —— 测试/老库的 order_lines 可能只有精简列，
            #   读的时候一律 .get()（缺列当没有，别炸整批导入）
            lines = [dict(r) for r in conn.execute(
                "SELECT * FROM order_lines WHERE document_no=? ORDER BY line_no",
                (dn,)).fetchall()]
            qty = sum(float(ln.get("quantity") or 0) for ln in lines) or 1
            names = [str(ln.get("item_name") or "") for ln in lines
                     if ln.get("item_name")]
            name = (names[0] if names else "") + (
                " 等%d件" % len(lines) if len(lines) > 1 else "")
            if not name:
                name = dn or "（无明细单）"
            codes = [str(ln.get("ean") or "") for ln in lines if ln.get("ean")]
            sns = [str(ln.get("sn") or "") for ln in lines if ln.get("sn")]
            amount = float(o["included_tax_amount"] or 0)
            if not math.isfinite(amount):
                # _clean_entry 那边是拒收；导入路径拒收会废掉整批，归 0 保住页面
                amount = 0.0
            # 商品行：一个订单行一行（金额没给就退 单价×数量，再退 订单合计/
            # 0）—— 后面 `_clean_products` 要求数字，这里必须落出数字。
            products = []
            for ln in lines:
                lamt = None
                if ln.get("included_tax_amount") not in (None, ""):
                    try:
                        lamt = float(ln["included_tax_amount"])
                    except (TypeError, ValueError):
                        lamt = None
                if lamt is None or not math.isfinite(lamt):
                    try:
                        lamt = float(ln.get("unit_price")) * float(ln.get("quantity") or 1)
                    except (TypeError, ValueError):
                        lamt = None
                if lamt is None or not math.isfinite(lamt):
                    lamt = amount if len(lines) == 1 else 0.0
                lqty = 1.0
                if ln.get("quantity") not in (None, ""):
                    try:
                        lqty = float(ln["quantity"])
                    except (TypeError, ValueError):
                        lqty = 1.0
                if not math.isfinite(lqty) or lqty <= 0:
                    lqty = 1.0
                # 商品行的 amount 是**单价**（订单行给的是行小计 ⇒ 除回去；
                # 应收/导出都按 数量 × 单价 算，两边口径才一致）
                unit = round(lamt / lqty, 2) if lqty else lamt
                products.append({
                    "name": str(ln.get("item_name") or "").strip()
                    or (name if len(lines) == 1 else dn),
                    "code": str(ln.get("ean") or "").strip(),
                    "sn": str(ln.get("sn") or "").strip(),
                    "quantity": lqty,
                    "amount": unit,
                    "category": "",        # 订单行没有品类来源，录完人再选
                })
            conn.execute(
                "INSERT INTO sale_entries (sold_at, goods_code, goods_name,"
                " quantity, amount, seller, note, source, sn, external_id,"
                " status, products, created_at, updated_at)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (o["doc_create_time"], "|".join(codes), name.strip(),
                 qty, round(amount, 2),
                 str(o["consumer_guide_name"] or ""), remark,
                 "linglong", "|".join(sns), dn, STAGED,
                 json.dumps(products, ensure_ascii=False), now, now))
            have.add(dn)
            imported += 1
    return {"ok": True, "imported": imported,
            "skipped_blacklist": skipped_black, "skipped_dup": skipped_dup}


# ---------------------------------------------------------------- 政策
def save_policy(root=None, rows: list = None, fetched_at: str = "", store_code=None) -> dict:
    """存一份政策快照（**按快照保留，不再先清后写** —— 用户 2026-09-30：
    「老的也保留可查」）。

    `rows` = `pmall.parse_policy` 的 dict 列表（表头原样，金额是文本不转）。
    每次调用 = 一份新快照（`fetched_at` 打时间戳）；反查 / 新鲜度 /
    导出的政策表都只认**最新那份**（见 `lookup` / `policy_meta`）。
    """
    from . import ownership
    rows = rows or []
    if not rows:
        return {"ok": False, "why": "没有行可存"}
    fmt = "%Y-%m-%d %H:%M:%S"
    fetched = (fetched_at or datetime.datetime.now(CST).strftime(fmt))
    written = 0
    with _db.tx(str(ensure(root))) as conn:
        conn.execute('BEGIN IMMEDIATE')
        denied = ownership.failure(root, store_code, write=True, conn=conn)
        if denied:
            return denied
        # ⚠ 快照边界 = `fetched_at`：**同一秒存两份会打平**（连点两次刷新、
        #   测试连存两份）⇒ 撞上就往后挪一秒，别让"最新一份"数不清。
        try:
            mx = conn.execute("SELECT MAX(fetched_at) FROM price_policy").fetchone()[0]
        except Exception:                                   # noqa: BLE001
            mx = None
        if mx and str(mx) >= fetched:
            try:
                fetched = (datetime.datetime.strptime(str(mx), fmt)
                           + datetime.timedelta(seconds=1)).strftime(fmt)
            except ValueError:
                pass                                        # 老格式认不出就并列（lookup 有 rowid 兜底）
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
    """按商品编码反查政策 —— **最新一份快照**。查不到回 `None`。

    ⚠ `rowid DESC` 是同一秒存两份时的兜底（`fetched_at` 打平了也认后写的）。
    """
    code = str(code or "").strip()
    if not code:
        return None
    conn = _db.open_db(str(ensure(root)))
    try:
        row = conn.execute(
            "SELECT * FROM price_policy WHERE goods_code=?"
            " ORDER BY fetched_at DESC, rowid DESC LIMIT 1", (code,)).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def policy_meta(root=None) -> dict:
    """政策表新鲜度（页面显示"上次刷新 + 多少行"）。没表/空表也回得体面值。

    ⚠ 行数 = **最新那份快照**的行数 —— 快照保留之后总数会随份数涨，
      报总数会让人以为政策本身变长了。
    """
    try:
        conn = _db.open_db(str(ensure(root)))
    except Exception:                                     # noqa: BLE001
        return {"rows": 0, "fetched_at": ""}
    try:
        row = conn.execute(
            "SELECT COUNT(*) AS n, MAX(fetched_at) AS f FROM price_policy"
            " WHERE fetched_at = (SELECT MAX(fetched_at) FROM price_policy)"
        ).fetchone()
        return {"rows": int(row["n"] or 0), "fetched_at": row["f"] or ""}
    except Exception:                                     # noqa: BLE001
        return {"rows": 0, "fetched_at": ""}
    finally:
        conn.close()


__all__ = [
    "SOURCES", "CATEGORIES", "STAGED", "SAVED", "db_path", "ensure",
    "save_entry", "delete_entry", "list_entries", "sellers",
    "exclude_entry", "entries_from_orders", "commit_entries",
    "save_policy", "lookup", "policy_meta",
]
