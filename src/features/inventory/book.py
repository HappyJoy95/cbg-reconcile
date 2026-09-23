"""盘点**取数** —— 「页面找数据抓取模块抓取最新的库存」的那一层。

用户 2026-09-20 定：**盘点页不碰云商 token**（它原来自己存一份、还要门店再登一次）。
现在页面只调本项目接口，由这里带着 `.secrets/erp.env` 那份账号去取数。

| 出口 | 给谁 |
|---|---|
| `warehouses()` | 盘点页的仓库下拉 |
| `store_for(whs, 本店名)` | **纯函数**：本店 → 那个仓（默认选中，省得选错隔壁店） |
| `book(date, store_id)` | 账面（在库 + 在途，串号级）—— 盘点的主数据 |
| `transit(date, store_id)` | 在途兜底（账面里没那一列时才用） |
| `index(date)` | 全库串号索引 —— 「表外码」能说出是哪个仓的货 |

⚠ **行原样返回**：`store_now()` 给的那些键（`Imei` / `ProCount_OnTransfer` / `OldFlag` …）
   正是页面 `core.js` 读的那套 ⇒ 归一化、uid、在途拆分**全在页面里那唯一一份口径里**，
   后端**不参与**任何"算"（算两份 = 迟早漂）。

⚠ **完整性自检在 `erp.store_now()` 里**（拉不全就抛 `ErpIncomplete`）——
   这里只做"包一层 + 出错给出人能看懂的中文"。
"""

from __future__ import annotations

import datetime
from typing import List, Optional

#: 全库索引只留这几列 —— 26,410 行原样回传要 ~9MB，
#: 而"这个码在哪个仓"只需要分仓 + 商品名 + 三个串号。
INDEX_KEYS = ("Store", "ProName", "Imei", "Imei2", "Imei3", "SubImei")


def _s(v) -> str:
    return str(v if v is not None else "").strip()


def client(verbose: bool = False):
    """建一个云商客户端（凭据走 `.secrets/erp.env` 那条链）。"""
    from ...erp import ErpClient
    return ErpClient(verbose=verbose)


def warehouses(verbose: bool = False, cl=None) -> dict:
    """仓库列表。返回 `{"ok", "warehouses", "why"}` —— **失败不抛**，让界面说清原因。"""
    c = cl or client(verbose=verbose)
    try:
        rows = c.warehouses()
    except Exception as e:                                     # noqa: BLE001
        return {"ok": False, "warehouses": [], "why": "%s: %s" % (type(e).__name__, e)}
    if not rows:
        return {"ok": False, "warehouses": [],
                "why": "云商没返回任何仓库 —— 账号权限或接口变了，别当成“店里没有仓”"}
    return {"ok": True, "warehouses": rows, "why": ""}


def store_for(whs: List[dict], store_name: str) -> Optional[dict]:
    """**本店 → 那个仓**（纯函数）。

    控制台知道"这台机器是哪家店"（`config/stores.yaml` 里的云商门店名），
    盘点盘的是**仓**，所以默认帮门店选好 —— 少一次手滑选到隔壁店的机会。

    ⚠ **只在能唯一确定时才给**：
      * 精确同名**多于一个** → `None`（存在同名仓，猜哪个都是错的）；
      * 只有一个"名字近似"的也不猜 —— 门店名和仓名差一个字（`…店` / `…店库`），
        猜错的表现是"账面看着正常、其实盘的是别人家的货"，最难发现。
      * 匹配不上 → `None`，界面就停在"请选择仓库"。
    """
    name = _s(store_name)
    if not name:
        return None
    for key in ("BranchName", "Name"):
        same = [w for w in whs or [] if _s(w.get(key)) == name]
        if len(same) == 1:
            return same[0]
        if len(same) > 1:
            return None
    # 名单里是「青岛正阳路利客来店」，仓那边有写「…店库」的 —— 只试这两种确定的后缀，
    # 不做模糊包含（"万达"能匹到三家店）。
    for alt in (name + "库", name + "店库", name[:-1] if name.endswith("店") else ""):
        if not alt or alt == name:
            continue
        for key in ("BranchName", "Name"):
            same = [w for w in whs or [] if _s(w.get(key)) == alt]
            if len(same) == 1:
                return same[0]
    return None


def book(date: str = "", store_id: str = "", verbose: bool = False, cl=None) -> dict:
    """**账面**（在库 + 在途，串号级）—— 盘点的主数据。

    `date` 是库存快照日（空 = 今天）。返回 `{"ok", "rows", "total", "date", "store_id", "why"}`。

    ⚠ **拉不全就 `ok=False`**（`ErpIncomplete`）：绝不让半份账面上场 ——
      少一半账面，扫到的机器会被判成「表外码」（窜货嫌疑），
      等于把"接口少给了"变成"门店台账有问题"。
    """
    c = cl or client(verbose=verbose)
    snap = _parse_date(date)
    try:
        got = c.store_now(snap, store_id=_s(store_id))
    except Exception as e:                                     # noqa: BLE001
        return {"ok": False, "rows": [], "total": 0, "date": snap.isoformat(),
                "store_id": _s(store_id), "why": "%s: %s" % (type(e).__name__, e)}
    return {"ok": True, "rows": got["rows"], "total": got["total"],
            "date": got["date"], "store_id": got["store_id"], "pages": got["pages"], "why": ""}


def transit(date: str = "", store_id: str = "", verbose: bool = False, cl=None) -> dict:
    """**在途兜底**（`InventoryImei` + `InventoryType=1`）。

    正常路径用不到它：账面那张表的 `ProCount_OnTransfer` 列就是在途。
    只有那一列拿不到（比如查了历史快照日）时才走这儿 ——
    Inventory Check 原来在浏览器里也是这么兜的。

    ⚠ 按仓过滤由**页面**再做一次（原逻辑就那样：接口没带仓库信息时宁可多展示也不漏），
      这里只负责把行取回来。
    """
    c = cl or client(verbose=verbose)
    snap = _parse_date(date)
    try:
        rows = c.transit_imei(snap, store_id=_s(store_id))
    except Exception as e:                                     # noqa: BLE001
        return {"ok": False, "rows": [], "why": "%s: %s" % (type(e).__name__, e)}
    return {"ok": True, "rows": rows, "why": ""}


def index(date: str = "", verbose: bool = False, cl=None) -> dict:
    """**全库串号索引**（全公司，不筛仓）—— 「表外码」据此说出"是哪个仓的货"。

    ⚠ 这里**只回传用得上的六列**（`INDEX_KEYS`）：26,410 行全字段约 9MB，
      而页面建索引只需要分仓 + 商品名 + 三个串号。
      ⇒ 页面那边建出来的 `Map` 跟原来**逐条一致**（同样的 `core.normCode` 去重）。
    """
    c = cl or client(verbose=verbose)
    snap = _parse_date(date)
    try:
        got = c.store_now(snap, store_id="")
    except Exception as e:                                     # noqa: BLE001
        return {"ok": False, "rows": [], "total": 0, "why": "%s: %s" % (type(e).__name__, e)}
    rows = [{k: r.get(k) for k in INDEX_KEYS} for r in got["rows"]]
    return {"ok": True, "rows": rows, "total": got["total"], "why": ""}


def _parse_date(text: str) -> datetime.date:
    """`YYYY-MM-DD` → date；空或写错**一律回今天**（界面上那个日期框是 type=date）。"""
    t = _s(text)
    if t:
        try:
            return datetime.date.fromisoformat(t[:10])
        except ValueError:
            pass
    return datetime.date.today()
