"""库存源决策 —— 映射表 + 设置 + 云商快照 → 逐行最终库存数（纯函数，无 IO）。

4.0.0 M-A2（开发目标「十一」：逐行粒度、手动=常驻固定值、默认跟随云商）。

输入三样：

* `mapping`  映射行 `[{"itemid","sku_id","title","pro_id","status"}, ...]`
              —— `pro_id` 空 = 缺映射；
* `overrides` `{sku_id: {"source": "manual", "value": <int>}}` —— **稀疏**：
              只有手动行才有条目，没出现的行 = 跟随云商（存量零操作）；
* `stock`    快照 `{pro_id: 在库数}`（`snapshot.build` 的产物）。

输出逐行 `[{"sku_id", "pro_id", "state", "source", "value", "why"}, ...]`，`state` 五态：

| state | 含义 | value |
|---|---|---|
| `cloud` | 跟随云商，快照有这个 ProId | 快照数 |
| `manual` | 手动填写（常驻固定值） | 手动值 |
| `missing_mapping` | 映射行没配 ProId | None |
| `missing_snapshot` | 跟随云商但快照没有这个 ProId（零库存商品也算有 key？见下） | None |
| `invalid_manual` | 手动值不合法（负数/非整数）—— **显式标红，不静默回退云商** | None |

⚠ `missing_snapshot` 的边界：快照是"在库串号聚出来的" —— **0 台在库的商品
不会有 key**。这跟"缺数据"是两回事：0 台就该传 0（平台也该下架可售）。
所以 `build` 里对映射涉及的 ProId 不补 0，由本函数区分：
`pro_id in stock` → 数；`不在` 但**快照本身新鲜** → 也是 0（state=cloud, value=0）。
⇒ 那 `missing_snapshot` 什么时候触发？**快照整体缺失/过期**（上游给 None）。
于是本函数签名收 `stock: Optional[dict]`：`None` = 快照不可用，
全部跟随云商的行标 `missing_snapshot`（手动行不受影响 —— 这正是手动行的价值）。
"""

from __future__ import annotations

from typing import Dict, List, Optional

SOURCE_CLOUD = "cloud"
SOURCE_MANUAL = "manual"

STATE_CLOUD = "cloud"
STATE_MANUAL = "manual"
STATE_NO_MAPPING = "missing_mapping"
STATE_NO_SNAPSHOT = "missing_snapshot"
STATE_BAD_MANUAL = "invalid_manual"

#: 不可用的映射状态 —— 验收轮（2026-09-24 v3 核对报告）实锤：
#: `miss`（匹不上）和 `weak`（弱匹配）行的 ProId 是**垃圾猜测**
#: （MatePad Pro Max → matebook 13、华为双肩包 → 联想拯救者包…），
#: **哪怕 ProId 非空也不许参与** —— 用了就是"把 A 商品的库存数传给 B 链接"。
#: 页面按 missing_mapping 显示，status 原值在行上，看得见为什么。
UNUSABLE_STATUS = ("miss", "weak")


def _manual_value(raw) -> Optional[int]:
    """手动值合法化：非负整数（允许 '12' / 12 / 12.0），非法 → None。

    ⚠ 不做"非法就回落云商"——那会把手改错藏起来（红线：显式失败好过静默换数）。
    """
    if raw is None or raw == "":
        return None
    try:
        f = float(raw)
    except (TypeError, ValueError):
        return None
    if f != int(f) or f < 0:
        return None
    return int(f)


def decide(mapping: List[dict],
           overrides: Optional[Dict[str, dict]] = None,
           stock: Optional[Dict[str, int]] = None) -> List[dict]:
    """逐行决策。三态输入 → 五态输出（见模块头表）。"""
    overrides = overrides or {}
    out: List[dict] = []
    for row in mapping:
        sku = str(row.get("sku_id") or "")
        pro = str(row.get("pro_id") or "").strip()
        base = {
            "sku_id": sku,
            "itemid": str(row.get("itemid") or ""),
            "title": str(row.get("title") or ""),
            "status": str(row.get("status") or ""),
            "pro_id": pro,
            # 静态展示属性（映射表透传；老表没有这两列 → 空串）
            "cloud_name": str(row.get("cloud_name") or ""),
            "spec": str(row.get("spec") or ""),
        }
        ov = overrides.get(sku) or {}
        want = str(ov.get("source") or SOURCE_CLOUD)
        if want == SOURCE_MANUAL:
            val = _manual_value(ov.get("value"))
            if val is None:
                out.append(dict(base, state=STATE_BAD_MANUAL, source=SOURCE_MANUAL,
                                value=None, why="手动值不是非负整数：%r" % (ov.get("value"),)))
            else:
                out.append(dict(base, state=STATE_MANUAL, source=SOURCE_MANUAL,
                                value=val, why="手动填写（常驻）"))
            continue
        # —— 跟随云商
        st = base["status"]
        if st in UNUSABLE_STATUS:                 # 垃圾 ProId 闸（见 UNUSABLE_STATUS）
            out.append(dict(base, state=STATE_NO_MAPPING, source=SOURCE_CLOUD,
                            value=None, why="status=%s 的映射不可信，不参与" % st))
            continue
        if not pro:
            out.append(dict(base, state=STATE_NO_MAPPING, source=SOURCE_CLOUD,
                            value=None, why="映射表没配云商 ProId"))
            continue
        if stock is None:
            out.append(dict(base, state=STATE_NO_SNAPSHOT, source=SOURCE_CLOUD,
                            value=None, why="云商快照不可用（没抓/过期）"))
            continue
        out.append(dict(base, state=STATE_CLOUD, source=SOURCE_CLOUD,
                        value=int(stock.get(pro, 0)),
                        why="" if pro in stock else "快照无此 ProId，按 0 台在库处理"))
    return out
