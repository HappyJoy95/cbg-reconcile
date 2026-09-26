# -*- coding: utf-8 -*-
"""备注里点名**别家门店**的行不算本店 —— 2.2.1 bug #4② 的通用判据。

用户 2026-09-26：麦凯乐 9 月新机系统算 40、公司手算 39 —— 差的那台是
9-11 一条**分销**（Mate X7，备注「丽达茂美团」），门店字段写麦凯乐、
实际是替丽达茂下的单。用户拍板：「备注其他门店的需要排除，
**不止针对麦凯乐和丽达茂**」⇒ 做成通用判据，不写死哪家店：

    备注（+单行备注）里出现**别家门店**的名字 ⇒ 这行整行不算本店。

⚠ 判据要点（每条都真踩过 / 实测过）：
* 门店名要认**短名** —— 「丽达茂美团」不含全名「青岛丽达茂店」，
  得把「青岛」前缀、「店」后缀剥掉才匹配得到；
* **自己的名字不算**（备注写本店名是常态：「麦凯乐自提」）；
* `绿城丽达店` 与「丽达茂」要分得开（前者短名是「绿城丽达」，不含「丽达茂」）；
* 平台岗 / 没 `erp_name` 的条目不参与（它们不是店）；
* 名单读不到 → 空索引 = **一个都不剔**（fail-open，回到旧口径，不凭空改数）。
"""

from __future__ import annotations

from typing import Dict, Iterable, Optional, Set

from ...paths import ROOT
from ...config_io import stores_table


def _variants(row: dict) -> Set[str]:
    """一家店在备注文本里可能被写成的样子（全名 / 去「青岛」/ 去「店」/ 都去）。"""
    out: Set[str] = set()
    for key in ("erp_name", "tdoc_name"):
        n = str((row or {}).get(key) or "").strip()
        if not n:
            continue
        no_city = n[2:] if n.startswith("青岛") else n
        no_shop = n[:-1] if n.endswith("店") else n
        both = no_city[:-1] if no_city.endswith("店") else no_city
        for v in (n, no_city, no_shop, both):
            if len(v) >= 2:            # 单字不成话（「店」这种），也防误伤
                out.add(v)
    return out


def name_index(root=None, rows: Optional[Iterable[dict]] = None) -> Dict[str, Set[str]]:
    """`{名字写法: 归属门店集合}` —— 判「备注里提到的是哪家店」用的倒排索引。

    ⚠ **倒排**（写法 → 店）而不是（店 → 写法）：一个写法可能同时属于两家店
      （撞名时），那种写法**有歧义就不拿来判**（`other_store_in` 里
      `store in owners` 直接放过）。
    ⚠ `rows=None` 才读 `config/stores.yaml`；测试可直接喂行。
    """
    if rows is None:
        rows = stores_table(root or ROOT)
    idx: Dict[str, Set[str]] = {}
    for r in rows or ():
        if not isinstance(r, dict):
            continue
        if str(r.get("kind") or "").strip() == "平台岗":
            continue                          # 平台岗是虚拟门店，不是店
        name = str(r.get("erp_name") or "").strip()
        if not name:
            continue                          # 没云商名的（颐高）进不了任何匹配
        for v in _variants(r):
            idx.setdefault(v, set()).add(name)
    return idx


def other_store_in(store: str, note, idx: Dict[str, Set[str]]) -> str:
    """`note` 里点名的**别家**门店 → 返回它的 `erp_name`；没点名返回 `''`。

    * 自己店名的写法命中 → 放过（`store in owners`）；
    * 撞名写法（同时属于本店和别家）→ 放过（歧义不判）；
    * 空备注 → 直接 `''`（绝大多数行走这条，别白扫 100 个子串）。
    """
    text = str(note or "").strip()
    if not text or not idx:
        return ""
    for variant, owners in idx.items():
        if variant in text and store not in owners:
            return sorted(owners)[0]
    return ""


__all__ = ["name_index", "other_store_in"]
