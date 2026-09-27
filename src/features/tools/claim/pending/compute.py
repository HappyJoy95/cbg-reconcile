# -*- coding: utf-8 -*-
"""待领清单 —— 按版本读取本地销售池，匹配活动机型、日期与领取状态。

正式版读 `erp_sales`，生活馆版读 `orders` × `order_lines`；均只读本地库。
⚠ 退货行不进待领；正式版可列无串号行，生活馆版只列有真 SN 的整机。
范围：调用方传 `stores=`（来自 `role_scope()`）。
"""

from __future__ import annotations

import datetime
import sqlite3
from pathlib import Path
from typing import List, Optional, Tuple

from .....paths import ROOT
from ..activities import catalog
from . import metric
from . import status as status_mod


def _f(x) -> float:
    try:
        return float(x or 0)
    except (TypeError, ValueError):
        return 0.0


def find_db(root) -> Optional[Path]:
    from .....app import data_state
    found = data_state._find_db(Path(root))
    return Path(found) if found else None


def _cols(conn: sqlite3.Connection) -> set:
    try:
        return {str(r[1]) for r in conn.execute("PRAGMA table_info(erp_sales)")}
    except sqlite3.Error:
        return set()


def load_stock_sn_map(db: Path) -> dict:
    """`erp_stock` **全部保留快照** → `{任意串号 → 真 SN}`。

    库存一行三列（`imei` / `sub_imei` / `sub_imei1`，外加主键 `sn`）：
    手机常见 **`imei`=86 码、`sub_imei`=真 SN**。销售侧只有 86 码时靠这张表反查。

    ⚠ **扫全部快照、不只最新一天** —— 卖出后机器就出库了，最新快照里没有；
      早几天的快照还留着映射（`purge` 默认留 30 天）。后写覆盖先写。
    ⚠ 只有**至少一列是真 SN** 的行才登记；三列全是 86 码的行不进表
    （反查不到就禁用在线领，别拿 IMEI 去打华为）。
    ⚠ 表/列不存在 → 空 dict（老库没跑过 `erp-dump` 也能打开待领页）。
    """
    out: dict = {}
    conn = sqlite3.connect("file:%s?mode=ro" % db, uri=True)
    try:
        conn.row_factory = sqlite3.Row
        try:
            have = {str(r[1]) for r in conn.execute(
                "PRAGMA table_info(erp_stock)")}
        except sqlite3.OperationalError:
            return {}
        code_cols = [c for c in ("sn", "imei", "sub_imei", "sub_imei1")
                     if c in have]
        if not code_cols:
            return {}
        select = ", ".join('"%s"' % c for c in code_cols)
        # 从旧到新：后写的快照覆盖先写的
        for r in conn.execute(
                'SELECT %s FROM erp_stock ORDER BY snapshot_date' % select):
            codes = [str(r[c] or "").strip() for c in code_cols]
            true_sn = metric.pick_true_sn(*codes)
            if not true_sn:
                continue
            for code in codes:
                if code and code != "-" and metric.sn_kind(code) == "imei":
                    out[code] = true_sn
                elif code and code == true_sn:
                    out[code] = true_sn
        return out
    finally:
        conn.close()


#: ⚠ 曾错把 **`串号标识`**（门店/机况标记 `W,新`）当成 SN —— 华为接口当然不认。
#:   真 SN 在云商的 **串号 / 串号1~3**（库里多为 `sn` / `串号`，**16 位**）。
SN_SQL_CANDIDATES = (
    "sn", "串号", "串号1", "串号2", "串号3",
    "Imei", "IMEI1", "Imei2", "Imei3",
)
NEVER_AS_SN = frozenset({"串号标识", "OldFlag", "old_flag", "发票号码", "单号"})

# 玲珑销售明细的 category_id 是内部编码。礼品等行即使填了串号、
# 商品名碰巧含活动机型，也不能成为待领商品；未知新编码仍交给机型与真 SN 判定。
LINGLONG_NON_DEVICE_CATEGORIES = frozenset({
    "ISRP12000001",          # 礼品
    "CMCG10000040",          # Care+
    "CMCG10000034",          # 移动电源
    "CMCG10000024",          # 路由器
    "CMCG10000140",          # 手机壳
    "CMCG10000037",          # 体脂秤
    "HWExclusiveAccessories",  # 专属配件
})


def _select_sql(conn: sqlite3.Connection) -> str:
    """按老库有没有列拼 SELECT —— 缺列补 `'' AS …`，别让整页 no such column。

    SN 相关列**全部选出**（`sn_raw0..n`），行级用 `metric.pick_device_sn`
    挑 16 位设备串号 —— 单列写死会猜错（有的行靠串号2/3）。
    """
    have = _cols(conn)
    base = ("SELECT 门店, 单据类型, 商品名称, 数量, 支付时间, 备注, 单行备注,"
            " 一级分类, 二级分类")
    doc = '"单号" AS doc' if "单号" in have else ("document_no AS doc"
                                              if "document_no" in have else "'' AS doc")
    who = "店员" if "店员" in have else "'' AS 店员"
    sn_parts = []
    for i, col in enumerate(SN_SQL_CANDIDATES):
        if col in have and col not in NEVER_AS_SN:
            sn_parts.append('"%s" AS sn_raw%d' % (col, i))
    if not sn_parts:
        sn_parts.append("'' AS sn_raw0")
    return (base + ", %s, %s, %s FROM erp_sales"
            " WHERE 支付时间 >= ? AND 支付时间 < ?"
            % (doc, who, ", ".join(sn_parts)))


def load_sales(db: Path, start: str, end: str, stores=None,
               stock_map: Optional[dict] = None) -> List[dict]:
    """窗口内 `erp_sales` 行（含无串号）。`end` 含当天。

    `stock_map`：86 码 → 真 SN（见 `load_stock_sn_map`）。
    行上多两个字段：
      * `sn`        —— 销售侧设备号（**状态键用它**，别改成反查结果）
      * `claim_sn`  —— 在线领取用：本来是 SN 就原样；86 码则库存反查，查不到空
    """
    want = set(stores) if stores is not None else None
    smap = stock_map or {}
    end_ex = (datetime.date.fromisoformat(end)
              + datetime.timedelta(days=1)).isoformat()
    conn = sqlite3.connect("file:%s?mode=ro" % db, uri=True)
    try:
        conn.row_factory = sqlite3.Row
        sql = _select_sql(conn)
        out = []
        for r in conn.execute(sql, (start, end_ex)):
            store = r["门店"] or ""
            if want is not None and store not in want:
                continue
            keys = r.keys()
            sn_cands = [r[k] for k in keys if str(k).startswith("sn_raw")]
            # 兜底：有的库列名不带 alias 前缀时再扫 SN 列
            if not sn_cands:
                sn_cands = [r[k] for k in keys if k in metric.SN_CANDIDATE_KEYS]
            # ⚠ SELECT 里 SN 列全部被 alias 成 `sn_raw0..n`（顺序 = SN_SQL_CANDIDATES）：
            #   raw0=sn 主键、raw1=串号 —— **状态键只看这两列**。
            #   若对全列 pick_device_sn，回填 串号3 后键会从 86 码跳成真 SN，已标已领会丢。
            primary_parts = [
                r[k] for k in ("sn_raw0", "sn_raw1")
                if k in keys
            ]
            if not primary_parts:
                primary_parts = sn_cands[:2]
            primary = metric.pick_device_sn(*primary_parts)
            if not primary:
                primary = metric.pick_device_sn(*sn_cands)
            # 在线领取：全部串号里挑真 SN；没有再库存反查
            claim_sn = metric.pick_true_sn(*sn_cands) or ""
            if not claim_sn:
                claim_sn = metric.resolve_claim_sn(primary, smap)
            if not claim_sn and metric.looks_like_imei(primary):
                # 主键是 86 码、副列/库存才有 SN
                claim_sn = metric.resolve_claim_sn(primary, smap)
            out.append({
                "store": store,
                "who": (r["店员"] or "").strip() if "店员" in keys else "",
                "typ": r["单据类型"] or "",
                "name": r["商品名称"] or "",
                "qty": _f(r["数量"]),
                "ts": r["支付时间"] or "",
                "doc": r["doc"] or "",
                "sn": primary,
                "claim_sn": claim_sn,
                "sn_kind": metric.sn_kind(primary),
                "c1": r["一级分类"] if "一级分类" in keys else "",
                "c2": r["二级分类"] if "二级分类" in keys else "",
                "note": " ".join(str(r[k] or "") for k in ("备注", "单行备注")
                                 if k in keys),
            })
        return out
    finally:
        conn.close()


def load_sales_linglong(db: Path, start: str, end: str) -> List[dict]:
    """生活馆版：玲珑销售单（`orders` × `order_lines`，池A）→ 与
    `load_sales` **同形状**的行，喂给同一个 `build_rows`。

    过滤口径（对齐 erp 侧语义，字段对玲珑）：
    * 窗口：`doc_create_time`（≈ 支付时间）在 [start, end+1)；
    * 有效单：`pay_status=2`（已付）且 `return_status=0`，
      且单号不在 `returns.related_doc_no` 里（部分退货的单）；
    * **整机**：排除已知礼品/服务/配件品类，且必须有可用的真 SN。
      礼品的 43 位标识虽然非空，却不能拿去领取；
    * 退货单不进待领（权益跟着原单）—— 有效单过滤已挡。

    `category_id` 不直接传给中文品类白名单；机型命中仍看商品名称。
    未知新编码保留真 SN + 机型匹配的机会，避免新品静默消失。
    """
    end_ex = (datetime.date.fromisoformat(end)
              + datetime.timedelta(days=1)).isoformat()
    conn = sqlite3.connect("file:%s?mode=ro" % db, uri=True)
    try:
        conn.row_factory = sqlite3.Row
        line_cols = {str(col[1]) for col in conn.execute(
            "PRAGMA table_info(order_lines)")}
        category_sql = ("l.category_id AS category_id" if "category_id" in line_cols
                        else "'' AS category_id")
        sql = (
            "SELECT o.document_no AS doc, o.store_name AS store,"
            " o.consumer_guide_name AS who, o.doc_create_time AS ts,"
            " l.item_name AS name, l.quantity AS qty, l.sn AS sn, %s"
            " FROM orders o JOIN order_lines l"
            "   ON l.document_no = o.document_no"
            " WHERE o.doc_create_time >= ? AND o.doc_create_time < ?"
            "   AND o.pay_status = 2 AND o.return_status = 0"
            "   AND l.sn <> ''"
            "   AND NOT EXISTS (SELECT 1 FROM returns r"
            "                    WHERE r.related_doc_no = o.document_no)"
        ) % category_sql
        out = []
        for r in conn.execute(sql, (start, end_ex)):
            category_id = str(r["category_id"] or "").strip()
            if category_id in LINGLONG_NON_DEVICE_CATEGORIES:
                continue
            sn = metric.pick_true_sn(r["sn"])
            if not sn:
                continue
            if metric.hard_excluded(r["name"] or ""):
                continue
            out.append({
                "store": r["store"] or "", "who": (r["who"] or "").strip(),
                "typ": "销售",              # 有效单已滤掉退货行
                "name": r["name"] or "", "qty": _f(r["qty"]),
                "ts": r["ts"] or "", "doc": r["doc"] or "",
                "sn": sn,
                "claim_sn": sn,
                "sn_kind": metric.sn_kind(sn),
                "c1": "", "c2": "",          # 内部编码进不了中文白名单（见 docstring）
                "note": "",
            })
        return out
    finally:
        conn.close()


def build_rows(sales: List[dict], activities: List[dict],
               statuses: Optional[dict] = None) -> Tuple[List[dict], List[dict]]:
    """销售行 → 待领行 + 被排除的退货行（方便 note 报数）。

    返回 `(pending_rows, skipped_returns)`。
    """
    pending: List[dict] = []
    skipped: List[dict] = []
    for s in sales:
        day = str(s.get("ts") or "")[:10]
        if metric.is_return(s.get("typ")):
            # 退货：若机型仍命中活动，记一笔「已退」旁注（不进待领主体）
            if catalog.matches_model_any(activities, s.get("name") or ""):
                skipped.append(dict(s, skip_reason="退货"))
            continue
        hits = metric.match_activities(
            s.get("name") or "", day, activities,
            c1=s.get("c1") or "", c2=s.get("c2") or "")
        if not hits:
            continue
        # 一笔多活动：拆成多行（不同权益分别领）
        for act in hits:
            row = dict(s)
            row["activity_id"] = act.get("id")
            row["activity_title"] = act.get("title") or act.get("benefit") or ""
            row["benefit"] = act.get("benefit") or ""
            row["value"] = act.get("value") or ""
            row["content"] = act.get("content") or ""
            row["fee"] = act.get("fee") or ""
            row["url"] = act.get("url") or ""
            row["after_claim"] = act.get("after_claim") or ""
            row["details"] = act.get("details") or ""
            row["act_start"] = act.get("start") or ""
            row["act_end"] = act.get("end") or ""
            row["category"] = act.get("category") or ""
            row["day"] = day
            row["status_key"] = metric.status_key(row) + "#" + str(act.get("id"))
            pending.append(row)
    joined = metric.join_status(pending, statuses)
    return metric.sort_rows(joined), skipped


def load(root=None, stores: Optional[List[str]] = None, day=None) -> dict:
    """一页数据：活动 + 待领行 + 合计。"""
    from ..... import edition as _edition          # ⚠ 5 个点 = src（本文件在 pending/ 下）
    lifehall = _edition.is_lifehall()
    root = Path(root or ROOT)
    acts = catalog.load_activities(root)
    statuses = status_mod.load(root)
    db = find_db(root)
    if not db:
        return {
            "ok": True,
            "rows": [],
            "summary": metric.summarize([]),
            "activities": acts,
            "note": ("还没有本地销售库 —— 先跑「抓取玲珑数据」。" if lifehall
                     else "还没有本地销售库 —— 先跑「抓取云商数据」。"),
            "src": "missing",
            "as_of": "",
        }
    # 窗口：取活动 start 最早 ～ 今天（或活动 end 最大），避免只扫本月漏掉跨月活动
    today = day or datetime.date.today()
    starts = [str(a.get("start") or "")[:10] for a in acts if a.get("start")]
    ends = [str(a.get("end") or "")[:10] for a in acts if a.get("end")]
    start = min([s for s in starts if s], default=None) or today.replace(day=1).isoformat()
    end = max([e for e in ends if e], default=None) or today.isoformat()
    if end > today.isoformat():
        end = today.isoformat()
    try:
        if lifehall:
            sales = load_sales_linglong(db, start, end)
            stock_map = {}                       # 没有云商库存表，86码反查不存在
        else:
            stock_map = load_stock_sn_map(db)
            sales = load_sales(db, start, end, stores=stores,
                               stock_map=stock_map)
    except Exception as e:  # noqa: BLE001 —— 说清楚，不拿空表假装成功
        return {
            "ok": False,
            "why": ("读玲珑销售单失败：%s" % e if lifehall
                    else "读 erp_sales 失败：%s" % e),
            "rows": [], "summary": metric.summarize([]), "activities": acts,
            "src": "failed", "as_of": "",
        }
    rows, skipped = build_rows(sales, acts, statuses)
    # as_of：窗口内 MAX(支付时间)
    as_of = max((str(r.get("ts") or "") for r in sales), default="")
    n_imei = sum(1 for r in rows if r.get("sn_kind") == "imei")
    n_resolved = sum(1 for r in rows
                     if r.get("sn_kind") == "imei" and r.get("claim_sn"))
    if lifehall:
        note = ("源：玲珑销售单（orders × order_lines）。匹配 = 商品名命中活动机型"
                " + 订单时间在赠送期内 + 有效设备 SN；排除礼品、服务、配件品类，"
                "退货不进待领。")
    else:
        note = ("源：仅 erp_sales（SQLite）。匹配 = **整机品类** + 商品名命中活动机型 + 支付日在赠送期内；"
                "手提袋/周边/延保单、退货不进待领。")
        if n_imei:
            note += (" 串号是 86 码的 %d 条：库存三列反查到真 SN %d 条可在线领，"
                     "其余只能手动领取/标已领。" % (n_imei, n_resolved))
    if skipped:
        note += " 另有 %d 笔退货命中机型（已排除）。" % len(skipped)
    return {
        "ok": True,
        "rows": rows,
        "summary": metric.summarize(rows),
        "activities": acts,
        "note": note,
        "src": "orders" if lifehall else "erp_sales",
        "as_of": as_of,
        "window": {"start": start, "end": end},
        "skipped_returns": len(skipped),
    }
