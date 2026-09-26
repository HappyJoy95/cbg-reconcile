"""生成器 —— 桥表 + 云商库存 → 天猫 6 列库存表（纯函数，无 IO）。

输入三样（都由上游给，本模块不取数）：

* `items`   天猫商品清单 `[(item_id, sku_id, title, code), ...]`（table.htm 拉的）；
* `bridge`  桥表 `{sku_id: pro_id}` —— **只存人工确认过的映射**（待核页产出）；
* `stock`   云商库存 `{pro_id: 在库数}`（inventory 取数按本店仓聚到 ProId）。

输出 `(rows, issues)`：

* `rows`    符合 `contract.HEADERS` 列序的行，可直接 `validate_rows` → 写 xlsx → 上传；
* `issues`  `[("unmapped_sku", sku_id, 说明), ...]` —— **没映射/没库存数据的一律
  不进表**（歧义不猜的姊妹条：宁可漏传一条，不传错一条）。
  漏传 = 平台库存停在旧值（下次补上）；传错 = 卖错货，回不来。
"""

from __future__ import annotations

from typing import Dict, Iterable, List, Tuple

from . import contract

Issue = Tuple[str, str, str]


def generate_rows(items: Iterable[Tuple[str, str, str, str]],
                  bridge: Dict[str, str],
                  stock: Dict[str, int],
                  store_id) -> Tuple[List[List], List[Issue]]:
    """拼整表。

    items 元素: (item_id, sku_id, title, code)；
    桥表键用 **skuId**（开发目标「三·五」：桥建在 skuId↔ProId 层，1 item ↔ N SKU）。
    """
    store = contract.excel_id(store_id)
    rows: List[List] = []
    issues: List[Issue] = []
    seen = set()
    for item_id, sku_id, title, code in items:
        try:
            sku = contract.excel_id(sku_id)
        except ValueError as e:
            issues.append(("bad_sku_id", str(sku_id), str(e)))
            continue
        if sku in seen:
            issues.append(("dup_sku", sku, "同一 skuId 出现两次，跳过后一条"))
            continue
        seen.add(sku)
        pro = bridge.get(sku)
        if not pro:
            issues.append(("unmapped_sku", sku, "桥表无此 skuId（待核未完成）：%s" % (title or "",)))
            continue
        if pro not in stock:
            # 云商没有这个编码的库存数据 ⇒ 不猜 0（0 = 平台下架，错了回不来）
            issues.append(("no_cloud_stock", sku, "云商无编码 %s 的库存数据" % (pro,)))
            continue
        rows.append(contract.build_row(item_id, store, sku, pro,
                                       _sku_label(title, code), int(stock[pro])))
    return rows, issues


def _sku_label(title: str, code: str) -> str:
    """E 列 sku 名称（给人核对用）：标题优先，回退编码。非必填，别塞复杂逻辑。"""
    t = (title or "").strip()
    if t:
        return t[:60]
    return (code or "").strip()


def summarize(issues: List[Issue]) -> str:
    """issues → 一行摘要（日志/界面用）。"""
    if not issues:
        return "无"
    counts: Dict[str, int] = {}
    for kind, _, _ in issues:
        counts[kind] = counts.get(kind, 0) + 1
    order = ["unmapped_sku", "no_cloud_stock", "dup_sku", "bad_sku_id"]
    parts = ["%s×%d" % (k, counts[k]) for k in order if k in counts]
    parts += ["%s×%d" % (k, v) for k, v in sorted(counts.items()) if k not in order]
    return " ".join(parts)
