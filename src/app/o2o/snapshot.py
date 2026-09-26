"""云商库存快照 —— **门店 + 商品编号** 两个键（4.0.0 M-A1）。

> 用户 2026-09-24 拍板：「匹配的话，看门店看商品编号的。」

## 口径（两句话）

* **门店**：本机配置的云商门店名（`config/store-*.yaml → erp_store_name`）
  对上快照里的**分仓名**（云商两张报表的 `Store`/`分仓` 字段装的都是「XX库」库名
  —— 2026-09-24 实测 `RptStoreNow` 44 个去重值，与门店名高度对应：
  顺和汇店↔顺和汇库、合美mall店↔胶南合美MALL库）。匹配规则见 `match_store`：
  **精确 → 核心词互含，唯一命中才自动选；多/零命中不猜**，交设置页下拉确认。
* **商品编号（ProId）**：快照行按 `store_name == 本店分仓` + `status == 在库`
  过滤后聚到 `{ProId: 台数}` —— 映射表的 `pro_id` 直接跟它对（开发目标「三·五」）。

## 数据源 = 本地池D（`erp_stock`，erp-dump 每日抓的天快照）

**不联网、不碰云商接口**。三条实测写死在这，别再改主意：

1. `RptStoreNow` 实时接口**当不了快照源**：2026-09-24 高频连打直接把云商账号
   挤下线（`ResponseID=9「您的账号在别处登录，您被迫下线」`），且它的 `Store`
   列值同样是库名 —— 比池D 不多任何维度，白拿一次封号风险；
2. 池D 行级字段够用：`pro_id`(100%非空) / `store_name`(分仓=库名) /
   `status`(在库23776·在途318) / `old_flag`(串号标识) / `pro_name`；
3. ⚠ `erp-api` 的「StoreName 假筛」说的是**查询参数**：传 `StoreName` 服务端
   不真过滤。这里**拉全量（SQL 不带仓条件）本地按行值分桶** —— 踩不到那个坑，
   注释留证：本地过滤，不依赖服务端筛选语义。

## 剔除（双判据，都有实测数字）

* `status != "在库"` → 剔（在途 318 行不掺）；
* `is_sample_marker(old_flag)`（样,新 3,850 + s,样,新…）→ 剔样机；
* `pro_name` 含「演示」或「样机」→ **也剔** —— 实测有 ~1,080 行
  （575 `新` + 387 `J,新` + 67 `办公机,新`…）**名字带演示/样机但标识不带样**，
  只按 `old_flag` 判会把真演示机放上网卖。开发目标「三」口径原文是
  「剔除演示机/样机」，双判据才是它的完整落地；宁可少一台库存。
"""

from __future__ import annotations

import datetime
import re
import sqlite3
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from ...pools import is_sample_marker      # 口径只有一份（features/…/rules.py 纯函数）
from ...dump import open_db, year_db       # 订单库读取/年度路径，别自己拼

#: 快照表（池D）
TABLE = "erp_stock"

#: 库名尾缀（库 → 核心词）
_LIB_SUFFIX = re.compile(r"库$")
#: 门店名前缀（「华为授权体验店-」「华为智能生活馆-」「华为臻选店·」… → 剥掉）+ 店尾
_SHOP_PREFIX = re.compile(r"^[^-·]+[-·]")
_SHOP_SUFFIX = re.compile(r"店$")


def shop_core(name: str) -> str:
    """门店名核心词：`华为授权体验店-顺和汇店` → `顺和汇`。纯文本归一，不猜。"""
    n = str(name or "").strip()
    if not n:
        return ""
    n = _SHOP_PREFIX.sub("", n)
    n = _SHOP_SUFFIX.sub("", n)
    return n


def lib_core(name: str) -> str:
    """分仓名核心词：`顺和汇库` → `顺和汇`；`胶南合美MALL库` → `胶南合美MALL`。"""
    n = str(name or "").strip()
    if not n:
        return ""
    return _LIB_SUFFIX.sub("", n)


def match_store(erp_store_name: str, store_names: List[str]) -> Tuple[str, List[str]]:
    """门店名 → 唯一分仓名。返回 `(命中或"", 候选列表)`。

    三档：① 精确相等 → 直接中；② 核心词**互含**（一方是另一方的子串，
    比较统一 lower）→ 恰好一个才中；③ 多命中/零命中 → 不猜，返回全部候选
    给设置页下拉（`candidates`），页面据此提示「请确认本店分仓」。
    """
    shop = str(erp_store_name or "").strip()
    names = [str(s or "").strip() for s in store_names if str(s or "").strip()]
    if not shop or not names:
        return "", names
    for s in names:                                    # ① 精确
        if s == shop:
            return s, names
    core = shop_core(shop).lower()
    if not core:
        return "", names
    hits = [s for s in names
            if core in lib_core(s).lower() or lib_core(s).lower() in core]   # ② 互含
    if len(hits) == 1:
        return hits[0], names
    return "", hits or names                           # ③ 不猜


def build(rows, store_name: str) -> Dict[str, int]:
    """快照行 → `{ProId: 在库台数}`（口径全在这，纯函数可单测）。

    `rows`：池D 某一天的行（dict，含 store_name/status/old_flag/pro_name/pro_id）。
    `store_name`：本店分仓名（空 → 返回空表，由 `load` 报「还没选」）。
    """
    sn = str(store_name or "").strip()
    if not sn:
        return {}
    stock: Dict[str, int] = {}
    for r in rows:
        # ① 门店（分仓）—— 拉全量本地分桶（见模块头「不踩 StoreName 假筛」）
        if str(r.get("store_name") or "").strip() != sn:
            continue
        # ② 只要在库，剔在途
        if str(r.get("status") or "").strip() != "在库":
            continue
        # ③ 样机（串号标识） + 演示机/样机（名称）—— 双判据，见模块头
        if is_sample_marker(r.get("old_flag")):
            continue
        name = str(r.get("pro_name") or "")
        if ("演示" in name) or ("样机" in name):
            continue
        # ④ 商品编号聚合
        pro = str(r.get("pro_id") or "").strip()
        if not pro:
            continue
        stock[pro] = stock.get(pro, 0) + 1
    return stock


def _no(why: str, **extra) -> dict:
    out = {"ok": False, "why": why, "stock": None, "day": "", "fresh": False,
           "stores": [], "store_name": "", "suggested": "", "rows_in_store": 0}
    out.update(extra)
    return out


def load(root, store_name: str = "", erp_store_name: str = "",
         *, today: Optional[datetime.date] = None) -> dict:
    """读本地池D最新快照 → 门店过滤聚合。

    返回（页面直接用）：

    * `ok=False` + `why`：没库 / 没快照 / 缺列 / 还没选分仓（`suggested` 给建议）——
      **失败全显式**，`stock=None` 会让 `source.decide` 把云商行标 `missing_snapshot`；
    * `ok=True`：`stock`、`day`、`fresh`（快照日 = 今天？）、`stores`（全部分仓，
      下拉用）、`store_name`（实际生效的分仓）、`rows_in_store`（本店行数，诊断）。
    """
    today_d = today or datetime.date.today()
    path = year_db(Path(root) / "out", today_d.year)
    if not path.exists():
        return _no("还没抓过数（缺 out/%s）—— 先跑 erp-dump" % path.name)
    conn = open_db(path)
    try:
        try:
            day = conn.execute("SELECT MAX(snapshot_date) FROM %s" % TABLE).fetchone()[0]
            if not day:
                return _no("erp_stock 还没有快照 —— 先跑 erp-dump")
            rows = [dict(r) for r in conn.execute(
                "SELECT pro_id, pro_name, store_name, status, old_flag"
                " FROM %s WHERE snapshot_date=?" % TABLE, (day,))]
        except sqlite3.OperationalError as e:
            return _no("erp_stock 缺列（重跑一次 erp-dump 会补齐）：%s" % e)
    finally:
        conn.close()
    stores = sorted({str(r.get("store_name") or "").strip() for r in rows
                     if str(r.get("store_name") or "").strip()})
    suggested, _ = match_store(erp_store_name, stores)
    sn = str(store_name or "").strip()
    if sn and sn not in stores:                        # 配置里存的分仓过期了（改名/换店）
        alt, _ = match_store(erp_store_name, stores)
        return _no("设置里的分仓 %r 不在最新快照里 —— 请重新选择" % sn,
                   stores=stores, suggested=alt)
    if not sn:
        return _no("还没选定本店分仓（设置页选择%s）"
                   % ("；已按门店名建议：%r" % suggested if suggested else ""),
                   stores=stores, suggested=suggested)
    stock = build(rows, sn)
    return {"ok": True, "why": "", "stock": stock, "day": str(day),
            "fresh": str(day) == today_d.isoformat(),
            "stores": stores, "store_name": sn, "suggested": suggested,
            "rows_in_store": sum(1 for r in rows
                                 if str(r.get("store_name") or "").strip() == sn)}
